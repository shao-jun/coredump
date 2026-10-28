# -*- coding: utf-8 -*-
import os
import csv
import glob
from db_init import get_connection
from import_csv import import_csv
import stack_stat

CRAWL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crawled_data')


def count_csv_rows(fpath):
    with open(fpath, 'r', encoding='utf-8') as f:
        return max(0, sum(1 for _ in csv.reader(f)) - 1)


def auto_import():
    conn = get_connection()
    conn.autocommit(True)
    cursor = conn.cursor()
    cursor.execute('USE coredump_db')

    csv_files = sorted(glob.glob(os.path.join(CRAWL_DIR, '*.csv')))
    if not csv_files:
        cursor.close()
        conn.close()
        return

    imported_any = False
    for fpath in csv_files:
        fname = os.path.basename(fpath)
        cursor.execute('SELECT id FROM imported_file WHERE file_name = %s', (fname,))
        if cursor.fetchone():
            continue

        file_rows = count_csv_rows(fpath)
        try:
            result = import_csv(fpath)
            cursor.execute(
                'INSERT INTO imported_file (file_name, file_rows, imported_rows) VALUES (%s, %s, %s)',
                (fname, file_rows, result['total'])
            )
            os.remove(fpath)
            imported_any = True
        except Exception as e:
            cursor.execute(
                'INSERT INTO imported_file (file_name, file_rows, imported_rows) VALUES (%s, %s, %s)',
                (fname, file_rows, 0)
            )

    cursor.close()
    conn.close()

    # 有新数据才重建汇总表，否则列表页数字会和 crash_info 对不上
    if imported_any:
        stack_stat.rebuild()


if __name__ == '__main__':
    auto_import()
