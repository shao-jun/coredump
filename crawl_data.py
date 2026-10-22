# -*- coding: utf-8 -*-
import requests
import csv
import os
from datetime import datetime, timedelta
from db_init import get_connection

CRAWL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crawled_data')
# Metabase 连接信息从环境变量读取，切勿硬编码到仓库中
METABASE_BASE = os.environ.get('METABASE_BASE', 'https://metabase.example.com')
METABASE_USER = os.environ.get('METABASE_USER', '')
METABASE_PASSWORD = os.environ.get('METABASE_PASSWORD', '')
DATABASE_ID = int(os.environ.get('METABASE_DATABASE_ID', 10))
SOURCE_TABLE = int(os.environ.get('METABASE_SOURCE_TABLE', 196))

PACKAGES = [
'kwin-x11',
'kwin-wayland',
'dde-dock',
'dde-session-ui',
'dde-session-shell',
'dde-launcher',
'dde-control-center',
]


def get_session():
    if not METABASE_USER or not METABASE_PASSWORD:
        raise RuntimeError(
            '缺少 Metabase 凭据：请在 {}/.env 中填写 METABASE_BASE / METABASE_USER / '
            'METABASE_PASSWORD（可参考 .env.example）'.format(
                os.path.dirname(os.path.abspath(__file__))))
    resp = requests.post(
        f'{METABASE_BASE}/api/session',
        json={'username': METABASE_USER, 'password': METABASE_PASSWORD},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()['id']


def build_query(package, date_str):
    return {
        "database": DATABASE_ID,
        "lib/type": "mbql/query",
        "stages": [{
            "filters": [
                ["=", {"lib/uuid": "495181f0-50d7-436e-bda2-6ac6f31a82d8", "effective-type": "type/Boolean"},
                    ["field", {"lib/uuid": "c5ba7ce1-bf57-4133-866b-a8a2b0d8a503", "effective-type": "type/Text", "base-type": "type/Text"}, 3536],
                    package],
                ["between", {"lib/uuid": "e516f8b6-2538-46d2-9a66-891aeab42108", "effective-type": "type/Boolean"},
                    ["field", {"lib/uuid": "ac3a7c8d-df3b-4f1c-86b3-5dbd0769ebc4", "effective-type": "type/Date", "base-type": "type/Date"}, 3578],
                    date_str, date_str],
		  ["<", {"lib/uuid": "eae15fc5-01d8-4ff7-9012-e9074b825f5c"},
			["field",{"lib/uuid": "35c1fac5-fc0c-4d5c-beee-94e13ac003fb","effective-type": "type/Integer","base-type": "type/Integer"},6663],
			2000],
            ],
            "lib/type": "mbql.stage/mbql",
            "order-by": [["desc", {"lib/uuid": "7b4b0958-44cc-4f73-8252-f894208d23ce"},
                ["field", {"lib/uuid": "bb823360-686c-4979-8048-e4432eb214cc", "effective-type": "type/Date", "base-type": "type/Date"}, 3578]]],
            "source-table": SOURCE_TABLE,
        }],
        "parameters": [],
    }


def crawl_package(session, package, date_str):
    headers = {'X-Metabase-Session': session, 'Content-Type': 'application/json'}
    resp = requests.post(
        f'{METABASE_BASE}/api/dataset',
        headers=headers,
        json=build_query(package, date_str),
        timeout=120
    )
    resp.raise_for_status()
    data = resp.json()

    rows = data.get('data', {}).get('rows', [])
    cols = [c.get('name') for c in data.get('data', {}).get('cols', [])]
    if not rows or not cols:
        return 0, None

    mapped = []
    for row in rows:
        mapped.append({k: (str(v) if v is not None else '') for k, v in dict(zip(cols, row)).items()})

    os.makedirs(CRAWL_DIR, exist_ok=True)
    date_file = date_str.replace('-', '')
    csv_path = os.path.join(CRAWL_DIR, f'{date_file}_{package}.csv')

    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(mapped)

    return len(mapped), csv_path


def parse_date(s):
    for fmt in ('%Y-%m-%d', '%Y%m%d'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    raise ValueError(f'日期格式不正确: {s}，应为 YYYY-MM-DD 或 YYYYMMDD')


def date_range(start_str, end_str):
    start = datetime.strptime(start_str, '%Y-%m-%d')
    end = datetime.strptime(end_str, '%Y-%m-%d')
    days = []
    cur = start
    while cur <= end:
        days.append(cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)
    return days


def get_latest_imported_date():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('USE coredump_db')
        cursor.execute('SELECT file_name FROM imported_file ORDER BY file_name DESC LIMIT 1')
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not row or not row[0]:
        return None

    # 文件名形如 20260920_kwin-x11.csv，前 8 位为日期
    date_part = row[0][:8]
    try:
        return datetime.strptime(date_part, '%Y%m%d').strftime('%Y-%m-%d')
    except ValueError:
        return None


def crawl(start_date=None, end_date=None):
    session = get_session()
    print('登录成功')

    if start_date and end_date:
        dates = date_range(start_date, end_date)
    else:
        latest = get_latest_imported_date()
        today = datetime.now().strftime('%Y-%m-%d')
        if latest:
            start = (datetime.strptime(latest, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
            if start > today:
                print(f'导入记录已是最新（最近导入 {latest}），无需爬取')
                return
            dates = date_range(start, today)
            print(f'检测到最近导入日期 {latest}，从 {start} 爬取到今天 {today}')
        else:
            dates = [today]
            print('未找到导入记录，默认爬取当天数据')

    for d in dates:
        print(f'--- {d} ---')
        for pkg in PACKAGES:
            print(f'  {pkg} ...', end=' ')
            try:
                count, path = crawl_package(session, pkg, d)
                if count > 0:
                    print(f'{count} 条 -> {path}')
                else:
                    print('无数据')
            except Exception as e:
                print(f'失败: {e}')

    print('爬取完成')


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3:
        s = parse_date(sys.argv[1])
        e = parse_date(sys.argv[2])
        crawl(s, e)
    elif len(sys.argv) == 1:
        crawl()
    else:
        print('用法:')
        print('  python3 crawl_data.py                    # 从最近导入日期的次日爬取到今天')
        print('  python3 crawl_data.py 2026-08-01 2026-08-25  # 爬取日期范围，按天逐个导出')
