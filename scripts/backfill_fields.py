# -*- coding: utf-8 -*-
"""把 Metabase 源表里的 arch / apt_id / baseline / cpu 回填到已有 crash_info 行。

为什么需要：这四列是 2026-11 才加进 crash_info 的，之前导入的老行只有
sys_version / crash_time / 包 / 栈，硬件字段全是 NULL。要按架构、机型基线、
CPU 看趋势就得把它们补上。

做法（不抓堆栈，只抓 5 个短字段，所以比重新爬一遍轻得多）：
  1. 从库里找出「有行但 arch 为空」的 (包, 月) 组合，只补这些，不补的不查；
  2. 按月向 Metabase 要「id, arch, apt_id, baseline, cpu」；单次超过
     MAX_ROWS 就自动按天、再按小时二分，避免被截断；
  3. 灌进临时表 tmp_crash_fields，UPDATE ... JOIN 按 crash_id 批量更新，
     避免 160 万次单行 UPDATE 的网络往返。

用法:
  python3 scripts/backfill_fields.py                       # 全部补齐
  python3 scripts/backfill_fields.py 2026-01-01 2026-11-17 # 只补指定区间
  python3 scripts/backfill_fields.py --dry-run             # 只看缺口，不联网
  python3 scripts/backfill_fields.py --package dde-dock    # 只补某个包
"""
import os
import sys
import time
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crawl_data as cd
from db_init import get_connection, load_env

load_env()

FIELD_IDS = {'id': 3577, 'date': 3578, 'package': 3536, 'dt': 4905,
             'arch': 6661, 'apt_id': 6677, 'baseline': 9422, 'cpu': 9423}
COLS = ('id', 'arch', 'apt_id', 'baseline', 'cpu')
TMP_DDL = """
CREATE TEMPORARY TABLE IF NOT EXISTS tmp_crash_fields (
    crash_id VARCHAR(255) NOT NULL PRIMARY KEY,
    arch VARCHAR(16),
    apt_id VARCHAR(64),
    baseline VARCHAR(32),
    cpu VARCHAR(64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
"""


def build_query(package, start, end, gran='date'):
    """只取 5 个短字段；gran='date' 按天/月筛，'dt' 按小时细分。"""
    def fld(name):
        return ["field", {"lib/uuid": str(uuid.uuid4()), "base-type": "type/Text"},
                FIELD_IDS[name]]

    filters = [
        ["=", {"lib/uuid": str(uuid.uuid4()), "effective-type": "type/Boolean"},
         ["field", {"lib/uuid": str(uuid.uuid4()), "base-type": "type/Text",
                    "effective-type": "type/Text"}, FIELD_IDS['package']],
         package],
        ["between", {"lib/uuid": str(uuid.uuid4()), "effective-type": "type/Boolean"},
         ["field", {"lib/uuid": str(uuid.uuid4()), "base-type": "type/Date",
                    "effective-type": "type/Date"}, FIELD_IDS[gran]],
         start, end],
    ]

    return {
        "database": cd.DATABASE_ID,
        "lib/type": "mbql/query",
        "stages": [{
            "lib/type": "mbql.stage/mbql",
            "source-table": cd.SOURCE_TABLE,
            "filters": filters,
            "fields": [fld(n) for n in COLS],
        }],
        "parameters": [],
    }


def fetch(session, package, start, end, gran='date'):
    """取一个时间段的记录；返回 (行, 是否可能被截断)。"""
    headers = {'X-Metabase-Session': session, 'Content-Type': 'application/json'}
    payload = build_query(package, start, end, gran)
    payload['constraints'] = {'max-results': cd.MAX_ROWS, 'max-results-bare-rows': cd.MAX_ROWS}
    resp = cd.HTTP.post(f'{cd.METABASE_BASE}/api/dataset', headers=headers,
                        json=payload, timeout=900)
    resp.raise_for_status()
    data = resp.json()
    if data.get('status') != 'completed':
        raise RuntimeError('查询失败: %s' % str(data.get('error'))[:300])
    rows = data['data']['rows']
    return rows, len(rows) >= cd.MAX_ROWS


def fetch_adaptive(session, package, start_dt, end_dt, depth=0):
    """二分到不触顶为止：月 -> 天 -> 6 小时 -> 小时。"""
    gran = 'date' if depth == 0 else 'dt'
    fmt = '%Y-%m-%d' if gran == 'date' else '%Y-%m-%d %H:%M:%S'
    rows, truncated = fetch(session, package, start_dt.strftime(fmt),
                            end_dt.strftime(fmt), gran)
    if not truncated:
        return rows
    if end_dt - start_dt <= timedelta(hours=1):
        print('    [警告] %s %s~%s 单小时仍触顶，可能缺数据'
              % (package, start_dt, end_dt))
        return rows
    mid = start_dt + (end_dt - start_dt) / 2
    left = fetch_adaptive(session, package, start_dt, mid, depth + 1)
    right = fetch_adaptive(session, package, mid + timedelta(seconds=1), end_dt, depth + 1)
    return left + right


def clip(v, n):
    if v is None:
        return None
    v = str(v).strip()
    return v[:n] if v else None


def month_gap(conn):
    """返回 [(包名, 'YYYY-MM', 总行数, 已填行数)]，只含有缺口的组合。"""
    cur = conn.cursor()
    cur.execute("USE coredump_db")
    cur.execute("""
        SELECT p.name, DATE_FORMAT(FROM_UNIXTIME(c.crash_time), '%Y-%m'),
               COUNT(*), SUM(c.arch IS NOT NULL)
        FROM crash_info c JOIN package_info p ON p.id = c.package_id
        WHERE c.crash_time IS NOT NULL
        GROUP BY p.name, 2
        HAVING SUM(c.arch IS NOT NULL) < COUNT(*)
        ORDER BY 2, 1
    """)
    out = [(r[0], r[1], r[2], r[3] or 0) for r in cur.fetchall()]
    cur.close()
    return out


def month_bounds(ym):
    start = datetime.strptime(ym + '-01', '%Y-%m-%d')
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, nxt - timedelta(days=1)


def apply_rows(conn, rows):
    cur = conn.cursor()
    cur.execute(TMP_DDL)
    cur.execute("TRUNCATE TABLE tmp_crash_fields")
    batch = []
    for r in rows:
        batch.append((str(r[0]).strip(), clip(r[1], 16), clip(r[2], 64),
                      clip(r[3], 32), clip(r[4], 64)))
        if len(batch) >= 5000:
            cur.executemany("INSERT IGNORE INTO tmp_crash_fields "
                            "(crash_id, arch, apt_id, baseline, cpu) VALUES (%s,%s,%s,%s,%s)", batch)
            batch = []
    if batch:
        cur.executemany("INSERT IGNORE INTO tmp_crash_fields "
                        "(crash_id, arch, apt_id, baseline, cpu) VALUES (%s,%s,%s,%s,%s)", batch)
    conn.commit()
    cur.execute("""
        UPDATE crash_info c JOIN tmp_crash_fields t ON c.crash_id = t.crash_id
        SET c.arch = t.arch, c.apt_id = t.apt_id, c.baseline = t.baseline, c.cpu = t.cpu
        WHERE c.arch IS NULL OR c.arch <> t.arch OR c.apt_id IS NULL
           OR c.baseline IS NULL OR c.cpu IS NULL
    """)
    n = cur.rowcount
    conn.commit()
    cur.close()
    return n


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry_run = '--dry-run' in sys.argv
    only_pkg = None
    if '--package' in sys.argv:
        only_pkg = sys.argv[sys.argv.index('--package') + 1]

    conn = get_connection()
    gaps = month_gap(conn)
    if len(args) >= 2:
        lo, hi = args[0][:7], args[1][:7]
        gaps = [g for g in gaps if lo <= g[1] <= hi]
    if only_pkg:
        gaps = [g for g in gaps if g[0] == only_pkg]
    if not gaps:
        print('没有需要回填的行')
        return

    total_gap = sum(g[2] - g[3] for g in gaps)
    print('需要回填 %d 个 (包,月) 组合，共 %d 行' % (len(gaps), total_gap))
    if dry_run:
        for name, ym, tot, filled in gaps:
            print('  %s %s 缺口 %d/%d' % (name, ym, tot - filled, tot))
        conn.close()
        return

    session = cd.get_session()
    t_all = time.time()
    done_rows = done_gap = 0
    for i, (name, ym, tot, filled) in enumerate(gaps, 1):
        start, end = month_bounds(ym)
        t0 = time.time()
        try:
            rows = fetch_adaptive(session, name, start, end)
        except Exception as e:
            print('[%d/%d] %s %s 查询失败: %s' % (i, len(gaps), name, ym, e))
            continue
        n = apply_rows(conn, rows) if rows else 0
        done_rows += n
        done_gap += tot - filled
        print('[%d/%d] %s %s 取回 %d 行，更新 %d 行（%.1fs）'
              % (i, len(gaps), name, ym, len(rows), n, time.time() - t0), flush=True)
    conn.close()
    print('完成：共更新 %d 行，覆盖缺口 %d 行，用时 %.1f 分钟'
          % (done_rows, done_gap, (time.time() - t_all) / 60))


if __name__ == '__main__':
    main()
