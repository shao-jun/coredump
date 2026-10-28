# -*- coding: utf-8 -*-
"""崩溃汇总表：把 155 万行 crash_info 预聚合成 6 万行的 stack_pv_stat。

列表页原来每次请求都要对整张 crash_info 做 GROUP BY + 排序（深分页 30 秒），
现在只读这张汇总表（几十毫秒）。导入/合并后调用 rebuild()/apply_merge() 刷新。

用法：
    python3 stack_stat.py            # 建表 + 索引 + 全量重建
    python3 stack_stat.py --schema   # 只建表和索引
"""
import os
import sys
import time

from db_init import get_connection

DDL = [
    '''CREATE TABLE IF NOT EXISTS stack_pv_stat (
        stack_key INT NOT NULL,
        package_id INT NOT NULL,
        version VARCHAR(64) NOT NULL DEFAULT '',
        sys_version VARCHAR(32) NOT NULL DEFAULT '',
        crash_count INT NOT NULL DEFAULT 0,
        last_crash_time INT NOT NULL DEFAULT 0,
        PRIMARY KEY (stack_key, package_id, version, sys_version),
        KEY idx_pkg (package_id),
        KEY idx_sys (sys_version),
        KEY idx_count (crash_count)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8''',
    '''CREATE TABLE IF NOT EXISTS stat_meta (
        k VARCHAR(64) NOT NULL PRIMARY KEY,
        v VARCHAR(64) NOT NULL DEFAULT ''
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8''',
    # 列表页默认排序（按崩溃数倒序、不筛选）专用的预聚合表：
    # 3.4 万行、按 crash_count 建索引，翻到第几百页也不用临时表排序。
    '''CREATE TABLE IF NOT EXISTS stack_stat (
        stack_key INT NOT NULL PRIMARY KEY,
        crash_count INT NOT NULL DEFAULT 0,
        last_crash_time INT NOT NULL DEFAULT 0,
        pkg_id INT DEFAULT NULL,
        versions TEXT,
        KEY idx_count (crash_count)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8''',
]

# crash_info 上补的索引：(sys_version, ...) 让系统版本筛选走索引，
# (package_id, crash_time) 让按包 + 时间段的趋势查询走索引。
INDEXES = [
    ('crash_info', 'idx_sysver', 'ADD INDEX idx_sysver (sys_version)'),
    ('crash_info', 'idx_pkg_time', 'ADD INDEX idx_pkg_time (package_id, crash_time)'),
    # 详情弹窗要取某栈涉及的可执行文件，覆盖索引可避免回表扫几十万行
    ('crash_info', 'idx_stack_binary', 'ADD INDEX idx_stack_binary (stack_id, binary_id)'),
    # 趋势弹窗按「某栈 + 某版本 + 时间段」统计，等值+IN+范围一体走索引
    ('crash_info', 'idx_pkg_stack_time', 'ADD INDEX idx_pkg_stack_time (package_id, stack_id, crash_time)'),
]


def _column_exists(cur, table, index_name):
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA='coredump_db' AND TABLE_NAME=%s AND INDEX_NAME=%s",
        (table, index_name))
    return cur.fetchone()[0] > 0


def ensure_schema(cur=None):
    """建汇总表 + 补索引，可重复执行。"""
    own = cur is None
    if own:
        conn = get_connection()
        conn.autocommit(True)
        cur = conn.cursor()
        cur.execute('USE coredump_db')
    for sql in DDL:
        cur.execute(sql)
    for table, name, clause in INDEXES:
        if not _column_exists(cur, table, name):
            cur.execute('ALTER TABLE %s %s' % (table, clause))
    if own:
        cur.close()
        conn.close()


def rebuild(verbose=True):
    """全量重建汇总表：建新表 -> 灌数 -> 原子改名，读请求不会看到半成品。"""
    t0 = time.time()
    conn = get_connection()
    conn.autocommit(True)
    cur = conn.cursor()
    cur.execute('USE coredump_db')
    ensure_schema(cur)

    cur.execute('DROP TABLE IF EXISTS stack_pv_stat_new')
    cur.execute('CREATE TABLE stack_pv_stat_new LIKE stack_pv_stat')
    cur.execute('''
        INSERT INTO stack_pv_stat_new
            (stack_key, package_id, version, sys_version, crash_count, last_crash_time)
        SELECT COALESCE(cs.same_stack, ci.stack_id),
               ci.package_id,
               COALESCE(pi.version, ''),
               COALESCE(ci.sys_version, ''),
               COUNT(*),
               MAX(ci.crash_time)
        FROM crash_info ci
        JOIN crash_stack cs ON cs.id = ci.stack_id
        JOIN package_info pi ON pi.id = ci.package_id
        GROUP BY COALESCE(cs.same_stack, ci.stack_id), ci.package_id,
                 COALESCE(pi.version, ''), COALESCE(ci.sys_version, '')
    ''')
    rows = cur.rowcount
    # 没有 stack_id 的崩溃（历史脏数据）不会出现在列表里，但首页总数要算上，
    # 把它单独记下来，避免总数比改造前少
    cur.execute('SELECT COUNT(*) FROM crash_info WHERE stack_id IS NULL')
    orphans = cur.fetchone()[0]
    cur.execute('SET SESSION group_concat_max_len = 1048576')
    cur.execute('DROP TABLE IF EXISTS stack_stat_new')
    cur.execute('CREATE TABLE stack_stat_new LIKE stack_stat')
    cur.execute('''
        INSERT INTO stack_stat_new
            (stack_key, crash_count, last_crash_time, pkg_id, versions)
        SELECT stack_key,
               SUM(crash_count),
               MAX(last_crash_time),
               CAST(SUBSTRING_INDEX(
                   GROUP_CONCAT(package_id ORDER BY crash_count DESC, package_id), ',', 1) AS UNSIGNED),
               GROUP_CONCAT(DISTINCT version ORDER BY version DESC SEPARATOR ', ')
        FROM stack_pv_stat
        GROUP BY stack_key
    ''')
    stack_rows = cur.rowcount
    cur.execute('RENAME TABLE stack_pv_stat TO stack_pv_stat_old, '
                'stack_pv_stat_new TO stack_pv_stat, '
                'stack_stat TO stack_stat_old, stack_stat_new TO stack_stat')
    cur.execute('DROP TABLE stack_pv_stat_old')
    cur.execute('DROP TABLE stack_stat_old')
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    cur.execute('REPLACE INTO stat_meta (k, v) VALUES (%s, %s)', ('rebuilt_at', stamp))
    cur.execute('REPLACE INTO stat_meta (k, v) VALUES (%s, %s)', ('orphan_crashes', str(orphans)))
    cur.close()
    conn.close()
    if verbose:
        print('汇总表重建完成：明细 %d 行 / 按栈 %d 行，耗时 %.1fs（%s）'
              % (rows, stack_rows, time.time() - t0, stamp))
    return rows


def apply_merge(new_key, old_key):
    """堆栈合并后，把汇总行从旧 key 挪到新 key（毫秒级，不用全量重建）。"""
    conn = get_connection()
    conn.autocommit(True)
    cur = conn.cursor()
    cur.execute('USE coredump_db')
    try:
        cur.execute('''SELECT package_id, version, sys_version, crash_count, last_crash_time
                       FROM stack_pv_stat WHERE stack_key = %s''', (old_key,))
        moved = cur.fetchall()
        # INSERT ... SELECT ... ON DUPLICATE 在 MariaDB 里会报列歧义，
        # 逐行用 VALUES 形式插入/累加
        for r in moved:
            cur.execute('''INSERT INTO stack_pv_stat
                               (stack_key, package_id, version, sys_version, crash_count, last_crash_time)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE
                               crash_count = stack_pv_stat.crash_count + VALUES(crash_count),
                               last_crash_time = GREATEST(stack_pv_stat.last_crash_time, VALUES(last_crash_time))''',
                        (new_key,) + tuple(r))
        cur.execute('DELETE FROM stack_pv_stat WHERE stack_key = %s', (old_key,))
        # 按栈汇总表同步重算这两行（versions 需要取并集）
        cur.execute('DELETE FROM stack_stat WHERE stack_key IN (%s, %s)', (new_key, old_key))
        cur.execute('''
            INSERT INTO stack_stat (stack_key, crash_count, last_crash_time, pkg_id, versions)
            SELECT stack_key, SUM(crash_count), MAX(last_crash_time),
                   CAST(SUBSTRING_INDEX(
                       GROUP_CONCAT(package_id ORDER BY crash_count DESC, package_id), ',', 1) AS UNSIGNED),
                   GROUP_CONCAT(DISTINCT version ORDER BY version DESC SEPARATOR ', ')
            FROM stack_pv_stat WHERE stack_key = %s GROUP BY stack_key
        ''', (new_key,))
    finally:
        cur.close()
        conn.close()


def rebuilt_at():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('USE coredump_db')
    try:
        cur.execute("SELECT v FROM stat_meta WHERE k = 'rebuilt_at'")
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    if '--schema' in sys.argv:
        conn = get_connection()
        conn.autocommit(True)
        cur = conn.cursor()
        cur.execute('USE coredump_db')
        ensure_schema(cur)
        cur.close()
        conn.close()
        print('表和索引已就绪')
    else:
        rebuild()
