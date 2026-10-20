import pymysql
import csv
import os
import re
import hashlib
from db_init import get_connection


def strip_addresses_from_stack(stack_text):
    """Strip header, #N 0xADDR prefix, and +0xoffset suffix from backtrace."""
    if not stack_text:
        return ''
    lines = stack_text.strip().split('\n')
    cleaned = []
    frame_pattern = re.compile(r'^\s*#\d+\s+0x[0-9a-fA-F]+\s+')
    offset_pattern = re.compile(r'\s\+\s+0x[0-9a-fA-F]+')
    for line in lines:
        line = line.strip()
        if not line or line.startswith('Stack trace of thread'):
            continue
        line = frame_pattern.sub('', line)
        line = offset_pattern.sub('', line)
        cleaned.append(line)
    return '\n'.join(cleaned)


def parse_storage_path(storage_path):
    """Parse systemd-coredump path for crash_hash, process_id, crash_time.
    Format: .../core.<binary>.<uid>.<crash_hash>.<pid>.<timestamp_ns>.lz4
    """
    if not storage_path:
        return None, None, None
    filename = os.path.basename(storage_path)
    parts = filename.split('.')
    if parts[-1] != 'lz4' or len(parts) < 7:
        return None, None, None
    timestamp_ns = parts[-2]
    process_id = parts[-3]
    crash_hash = parts[-4]
    crash_time_s = None
    if timestamp_ns and timestamp_ns.isdigit():
        crash_time_s = int(timestamp_ns[:10])
    return crash_hash, process_id, crash_time_s




def get_or_insert(cursor, table, unique_fields, insert_fields, insert_values):
    where = ' AND '.join(f"{k}=%s" for k in unique_fields)
    where_vals = tuple(unique_fields.values())
    cursor.execute(f"SELECT id FROM {table} WHERE {where} LIMIT 1", where_vals)
    row = cursor.fetchone()
    if row:
        return row[0]
    placeholders = ', '.join(['%s'] * len(insert_fields))
    cols = ', '.join(insert_fields)
    cursor.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", insert_values)
    return cursor.lastrowid


def import_csv(csv_path, update_existing=False):
    conn = get_connection()
    conn.autocommit(False)
    cursor = conn.cursor()
    cursor.execute('USE coredump_db')

    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            count = 0
            skipped = 0
            for row in reader:
                row = {k.lower().replace(' ', '_'): v for k, v in row.items()}
                crash_id = row.get('id', '').strip()
                if not crash_id:
                    continue

                sys_version = row.get('sys_v_number', '').strip()
                pkg_name = row.get('package', '').strip()
                pkg_version = row.get('version', '').strip()
                storage_path = row.get('storagepath', '').strip()
                buildid = row.get('buildid', '').strip()
                stack_raw = row.get('stackinfo', '').strip()
                exe_path = row.get('exe', '').strip()

                crash_hash, process_id, crash_time = parse_storage_path(storage_path)

                package_id = get_or_insert(
                    cursor, 'package_info',
                    {'name': pkg_name, 'version': pkg_version},
                    ['name', 'version'],
                    (pkg_name, pkg_version)
                )

                binary_id = None
                if exe_path:
                    binary_id = get_or_insert(
                        cursor, 'binary_info',
                        {'package_id': package_id, 'binary_name': exe_path},
                        ['package_id', 'binary_name'],
                        (package_id, exe_path)
                    )

                stack_id = None
                if crash_hash and stack_raw:
                    stack_clean = strip_addresses_from_stack(stack_raw)
                    if not stack_clean:
                        continue
                    stack_md5 = hashlib.md5(stack_clean.encode('utf-8')).hexdigest()
                    cursor.execute(
                        "SELECT cs.id, cs.stack_md5 FROM crash_hash ch JOIN crash_stack cs ON cs.id = ch.stack_id WHERE ch.crash_hash = %s",
                        (crash_hash,)
                    )
                    ch_row = cursor.fetchone()
                    if ch_row and ch_row[1] == stack_md5:
                        stack_id = ch_row[0]
                    else:
                        cursor.execute(
                            "SELECT id FROM crash_stack WHERE stack_md5 = %s LIMIT 1",
                            (stack_md5,)
                        )
                        s_row = cursor.fetchone()
                        if s_row:
                            stack_id = s_row[0]
                        else:
                            cursor.execute(
                                "INSERT INTO crash_stack (stack_md5, stack_text) VALUES (%s, %s)",
                                (stack_md5, stack_clean)
                            )
                            stack_id = cursor.lastrowid
                        if not ch_row:
                            cursor.execute(
                                "INSERT INTO crash_hash (crash_hash, stack_id) VALUES (%s, %s)",
                                (crash_hash, stack_id)
                            )
                        elif ch_row and ch_row[1] != stack_md5:
                            cursor.execute(
                                "INSERT IGNORE INTO crash_hash (crash_hash, stack_id) VALUES (%s, %s)",
                                (crash_hash, stack_id)
                            )

                symbol_id = None
                if buildid:
                    symbol_id = get_or_insert(
                        cursor, 'symbol_info',
                        {'buildid': buildid},
                        ['buildid'],
                        (buildid,)
                    )

                cursor.execute(
                    "SELECT id FROM crash_info WHERE crash_id = %s LIMIT 1",
                    (crash_id,)
                )
                existing = cursor.fetchone()
                if existing:
                    if update_existing:
                        cursor.execute(
                            "UPDATE crash_info SET sys_version=%s, crash_time=%s, package_id=%s, binary_id=%s, stack_id=%s, process_id=%s, symbol_id=%s WHERE crash_id=%s",
                            (sys_version, crash_time, package_id, binary_id,
                             stack_id, process_id, symbol_id, crash_id)
                        )
                        count += 1
                    else:
                        skipped += 1
                else:
                    cursor.execute(
                        "INSERT INTO crash_info (crash_id, sys_version, crash_time, package_id, binary_id, stack_id, process_id, symbol_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (crash_id, sys_version, crash_time, package_id, binary_id,
                         stack_id, process_id, symbol_id)
                    )
                    count += 1

            conn.commit()
            return {"total": count, "skipped": skipped}
    except Exception as e:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("用法: python import_csv.py <csv文件路径>")
        sys.exit(1)
    import_csv(sys.argv[1])
