# -*- coding: utf-8 -*-
"""重爬历史数据：补回被 Metabase 2000 行上限砍掉的崩溃记录。

背景：crawl_data.py 早期每次请求只拿回 2000 行（Metabase 的
max-results-bare-rows 系统设置），单日超过 2000 次崩溃的包，多出来的行
从未入库。现在 crawl_package 已带 constraints 解除上限，可以按天重抓。

安全约定（用户手工改过的东西不能被覆盖）：
  1. 只做 INSERT，命中已有 crash_id 直接跳过（import_csv 的 update_existing=False），
     crash_info 上已有的行一个字段都不改；
  2. 开头把 crash_stack 的可编辑列（resolved/code_link/reason/resolved_version/
     same_stack）快照进 crash_stack_backup_<日期>，作为兜底；
  3. 抓下来的 CSV 写到临时目录，抓一条删一条，不碰 crawled_data/ 与导入日志。

用法:
  python3 scripts/recrawl_history.py --min-count 1000        # 重抓单日>=1000行的天
  python3 scripts/recrawl_history.py --min-count 0           # 全部天都重抓
  python3 scripts/recrawl_history.py --package dde-dock --limit 20
  python3 scripts/recrawl_history.py --gap-days              # 只补库里缺失的日期
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crawl_data as cd
import import_csv as imp
from db_init import get_connection, load_env

load_env()

SCRATCH = os.path.expanduser('~/.hermes/cache/scratch')
TMP_CSV = os.path.join(SCRATCH, 'recrawl_csv')
PROGRESS = os.path.join(SCRATCH, 'recrawl_progress.json')
cd.CRAWL_DIR = TMP_CSV
os.makedirs(TMP_CSV, exist_ok=True)


def arg(name, default=None, cast=str):
    if name in sys.argv:
        return cast(sys.argv[sys.argv.index(name) + 1])
    return default


def load_progress():
    if os.path.exists(PROGRESS):
        with open(PROGRESS) as f:
            return set(json.load(f))
    return set()


def save_progress(done):
    with open(PROGRESS, 'w') as f:
        json.dump(sorted(done), f)


def backup_stack_edits(conn, tag=None):
    """把手工改过的栈字段存一份，防止意外覆盖。"""
    tag = tag or datetime.now().strftime('%Y%m%d')
    table = 'crash_stack_backup_%s' % tag
    cur = conn.cursor()
    cur.execute("USE coredump_db")
    cur.execute("""SELECT COUNT(*) FROM information_schema.TABLES
                   WHERE TABLE_SCHEMA='coredump_db' AND TABLE_NAME=%s""", (table,))
    if cur.fetchone()[0]:
        print('备份已存在:', table)
    else:
        cur.execute("""CREATE TABLE %s AS
                       SELECT id, stack_md5, resolved, code_link, reason,
                              resolved_version, same_stack
                       FROM crash_stack""" % table)
        conn.commit()
        print('已备份 %d 行到 %s' % (cur.rowcount, table))
    cur.close()
    return table


def work_items(conn, min_count, only_pkg, gap_days_only):
    cur = conn.cursor()
    cur.execute("USE coredump_db")
    items = []
    if not gap_days_only:
        sql = """SELECT p.name, DATE(FROM_UNIXTIME(c.crash_time)) d, COUNT(*) cnt
                 FROM crash_info c JOIN package_info p ON p.id = c.package_id
                 WHERE c.crash_time IS NOT NULL
                 GROUP BY p.name, d HAVING COUNT(*) >= %s ORDER BY d, p.name"""
        cur.execute(sql, (min_count,))
        items = [(r[0], r[1].strftime('%Y-%m-%d'), r[2]) for r in cur.fetchall()]
        if only_pkg:
            items = [i for i in items if i[0] == only_pkg]

    # 库里还没有的日期（通常是最近几天没爬上）
    cur.execute("""SELECT p.name, MAX(DATE(FROM_UNIXTIME(c.crash_time)))
                   FROM crash_info c JOIN package_info p ON p.id = c.package_id
                   GROUP BY p.name""")
    today = datetime.now().date()
    for name, last in cur.fetchall():
        if only_pkg and name != only_pkg:
            continue
        if not last:
            continue
        last = last if isinstance(last, datetime) else datetime.combine(last, datetime.min.time())
        cur_day = last.date() + timedelta(days=1)
        while cur_day <= today:
            items.append((name, cur_day.strftime('%Y-%m-%d'), 0))
            cur_day += timedelta(days=1)
    cur.close()
    # 去重，按日期排序
    uniq = {}
    for name, day, cnt in items:
        uniq[(name, day)] = cnt
    return sorted(((n, d, c) for (n, d), c in uniq.items()), key=lambda x: (x[1], x[0]))


def main():
    min_count = arg('--min-count', 1000, int)
    only_pkg = arg('--package')
    limit = arg('--limit', 0, int)
    gap_only = '--gap-days' in sys.argv
    no_rebuild = '--no-rebuild' in sys.argv

    conn = get_connection()
    backup_stack_edits(conn)
    items = work_items(conn, min_count, only_pkg, gap_only)
    done = load_progress()
    todo = [i for i in items if '%s|%s' % (i[0], i[1]) not in done]
    if limit:
        todo = todo[:limit]
    print('待重抓 %d 条 (包,日)（已完成 %d，总候选 %d），阈值 单日>=%d 行'
          % (len(todo), len(done), len(items), min_count), flush=True)

    session = cd.get_session()
    t_all = time.time()
    added = 0
    for i, (name, day, cnt) in enumerate(todo, 1):
        t0 = time.time()
        try:
            n, path = cd.crawl_package(session, name, day)
            res = {'total': 0, 'skipped': 0}
            if n and path:
                res = imp.import_csv(path, update_existing=False)
        except Exception as e:
            print('[%d/%d] %s %s 失败: %s' % (i, len(todo), name, day, e), flush=True)
            continue
        finally:
            p = os.path.join(TMP_CSV, '%s_%s.csv' % (day.replace('-', ''), name))
            if os.path.exists(p):
                os.remove(p)
        added += res['total']
        done.add('%s|%s' % (name, day))
        save_progress(done)
        print('[%d/%d] %s %s 库里%d行 抓到%d 新增%d 跳过%d (%.1fs)'
              % (i, len(todo), name, day, cnt, n or 0, res['total'],
                 res['skipped'], time.time() - t0), flush=True)

    conn.close()
    print('抓取完成：共新增 %d 行，用时 %.1f 分钟' % (added, (time.time() - t_all) / 60), flush=True)

    if not no_rebuild:
        print('重建汇总表 ...', flush=True)
        import stack_stat
        stack_stat.rebuild()
        print('汇总表重建完成', flush=True)


if __name__ == '__main__':
    main()
