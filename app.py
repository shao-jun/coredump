import os
from flask import Flask, render_template, jsonify, request
from db_init import get_connection

app = Flask(__name__)


def get_db():
    conn = get_connection()
    conn.autocommit(True)
    cursor = conn.cursor()
    cursor.execute('USE coredump_db')
    return conn, cursor


@app.route('/')
def index():
    conn, cursor = get_db()
    cursor.execute('SELECT COUNT(*) FROM crash_info')
    total = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM crash_stack WHERE resolved = 0 AND same_stack IS NULL')
    unresolved = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM crash_stack WHERE resolved = 1 AND same_stack IS NULL')
    resolved = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM crash_stack WHERE resolved = 2 AND same_stack IS NULL')
    wontfix = cursor.fetchone()[0]
    cursor.execute('''
        SELECT pi.name, pi.version, COUNT(ci.id) as cnt
        FROM crash_info ci
        JOIN package_info pi ON ci.package_id = pi.id
        GROUP BY ci.package_id ORDER BY cnt DESC LIMIT 10
    ''')
    top_packages = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('index.html',
                           total=total, unresolved=unresolved, resolved=resolved,
                           wontfix=wontfix, top_packages=top_packages)


@app.route('/api/crashes')
def api_crashes():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '').strip()
    pkg_filter = request.args.get('package', '').strip()
    ver_filter = request.args.get('version', '').strip()
    status_filter = request.args.get('status', '').strip()
    offset = (page - 1) * per_page

    conn, cursor = get_db()
    where_clauses = []
    having_clauses = []
    params = []

    if search:
        where_clauses.append('(cs.stack_md5 LIKE %s OR LEFT(cs.stack_text, 200) LIKE %s)')
        params.extend([f'%{search}%', f'%{search}%'])
    if pkg_filter:
        where_clauses.append('pi.name = %s')
        params.append(pkg_filter)
    if ver_filter:
        where_clauses.append('pi.version = %s')
        params.append(ver_filter)
    if status_filter == 'resolved':
        having_clauses.append('MAX(cs.resolved) = 1')
    elif status_filter == 'wontfix':
        having_clauses.append('MAX(cs.resolved) = 2')
    elif status_filter == 'unresolved':
        having_clauses.append('MAX(cs.resolved) = 0')

    where_sql = (' WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''
    having_sql = (' HAVING ' + ' AND '.join(having_clauses)) if having_clauses else ''

    cursor.execute(f'''
        SELECT COUNT(*) FROM (
            SELECT cs.id
            FROM crash_info ci
            JOIN crash_stack cs ON ci.stack_id = cs.id
            LEFT JOIN crash_stack cs2 ON cs.same_stack = cs2.id
            JOIN package_info pi ON ci.package_id = pi.id
            {where_sql}
            GROUP BY COALESCE(cs2.id, cs.id)
        ) t
    ''', params)
    total = cursor.fetchone()[0]

    cursor.execute(f'''
        SELECT COALESCE(cs2.id, cs.id), COALESCE(cs2.stack_md5, cs.stack_md5),
               COALESCE(cs2.stack_text, cs.stack_text),
               COALESCE(cs2.resolved, cs.resolved) as resolved,
               COALESCE(cs2.code_link, cs.code_link) as code_link,
               COUNT(ci.id) as crash_count,
               MAX(FROM_UNIXTIME(ci.crash_time, "%%Y-%%m-%%d %%H:%%i:%%s")) as last_crash_time,
               pi.name as pkg_name,
               MAX(ci.process_id) as process_id,
               GROUP_CONCAT(DISTINCT pi.version ORDER BY pi.version DESC SEPARATOR ', ') as versions
        FROM crash_info ci
        JOIN crash_stack cs ON ci.stack_id = cs.id
        LEFT JOIN crash_stack cs2 ON cs.same_stack = cs2.id
        JOIN package_info pi ON ci.package_id = pi.id
        {where_sql}
        GROUP BY COALESCE(cs2.id, cs.id)
        {having_sql}
        ORDER BY crash_count DESC
        LIMIT %s OFFSET %s
    ''', params + [per_page, offset])
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    crashes = []
    for r in rows:
        stack_preview = r[2][:120].replace('\n', ' | ') if r[2] else ''
        crashes.append({
            'id': r[0], 'stack_md5': r[1], 'stack_preview': stack_preview,
            'resolved': r[3], 'code_link': r[4], 'crash_count': r[5],
            'last_crash_time': r[6], 'pkg_name': r[7], 'process_id': r[8],
            'versions': r[9],
        })

    return jsonify({
        'total': total, 'page': page, 'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
        'data': crashes
    })


@app.route('/api/stack/<int:stack_pk>')
def api_stack_detail(stack_pk):
    conn, cursor = get_db()
    cursor.execute('''
        SELECT cs.id, cs.stack_md5, cs.stack_text, cs.symbolicated_stack, cs.resolved, cs.code_link, cs.reason, cs.resolved_version,
               COUNT(ci.id) as crash_count,
               GROUP_CONCAT(DISTINCT bi.binary_name SEPARATOR ', ') as binaries,
               GROUP_CONCAT(DISTINCT pi.name ORDER BY pi.name SEPARATOR ', ') as pkg_name
        FROM crash_stack cs
        JOIN crash_info ci ON ci.stack_id = cs.id
        JOIN package_info pi ON ci.package_id = pi.id
        LEFT JOIN binary_info bi ON ci.binary_id = bi.id
        WHERE cs.id = %s
        GROUP BY cs.id
    ''', (stack_pk,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return jsonify({'error': 'not found'}), 404

    cursor.execute('''
        SELECT pi.version, COUNT(ci.id) as cnt
        FROM crash_info ci
        JOIN package_info pi ON ci.package_id = pi.id
        WHERE ci.stack_id = %s
        GROUP BY pi.version
        ORDER BY cnt DESC
    ''', (stack_pk,))
    version_rows = cursor.fetchall()
    cursor.close()
    conn.close()

    return jsonify({
        'id': row[0], 'stack_md5': row[1], 'stack_text': row[2],
        'symbolicated_stack': row[3],
        'resolved': row[4], 'code_link': row[5], 'reason': row[6], 'resolved_version': row[7], 'crash_count': row[8],
        'binaries': row[9], 'pkg_name': row[10],
        'version_list': [{'version': r[0], 'count': r[1]} for r in version_rows],
    })


@app.route('/api/crash/<int:crash_pk>')
def api_crash_detail(crash_pk):
    conn, cursor = get_db()
    cursor.execute('''
        SELECT ci.id, ci.crash_id, ci.sys_version,
               FROM_UNIXTIME(ci.crash_time, "%%Y-%%m-%%d %%H:%%i:%%s") as crash_time_str,
               pi.name, pi.version, bi.binary_name, ci.process_id,
               si.buildid,
               cs.stack_text, cs.resolved, cs.code_link, cs.stack_md5
        FROM crash_info ci
        LEFT JOIN package_info pi ON ci.package_id = pi.id
        LEFT JOIN binary_info bi ON ci.binary_id = bi.id
        LEFT JOIN symbol_info si ON ci.symbol_id = si.id
        LEFT JOIN crash_stack cs ON ci.stack_id = cs.id
        WHERE ci.id = %s
    ''', (crash_pk,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return jsonify({'error': 'not found'}), 404

    return jsonify({
        'id': row[0], 'crash_id': row[1], 'sys_version': row[2], 'crash_time': row[3],
        'pkg_name': row[4], 'pkg_version': row[5], 'binary_name': row[6],
        'process_id': row[7], 'buildid': row[8],
        'stack_text': row[9], 'resolved': row[10], 'code_link': row[11],
        'stack_md5': row[12],
    })


@app.route('/api/packages')
def api_packages():
    conn, cursor = get_db()
    cursor.execute('''
        SELECT pi.id, pi.name, pi.version, COUNT(ci.id) as crash_count
        FROM package_info pi
        LEFT JOIN crash_info ci ON ci.package_id = pi.id
        GROUP BY pi.id ORDER BY crash_count DESC
    ''')
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([{'id': r[0], 'name': r[1], 'version': r[2], 'crash_count': r[3]} for r in rows])


@app.route('/api/package/versions')
def api_package_versions_by_name():
    name = request.args.get('name', '').strip()
    conn, cursor = get_db()
    cursor.execute('''
        SELECT pi.version, COUNT(ci.id) as crash_count,
               SUM(CASE WHEN cs.resolved = 1 THEN 1 ELSE 0 END) as resolved_count
        FROM crash_info ci
        JOIN package_info pi ON ci.package_id = pi.id
        LEFT JOIN crash_stack cs ON ci.stack_id = cs.id
        WHERE pi.name = %s
        GROUP BY pi.version
        ORDER BY pi.version DESC
    ''', (name,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([{'version': r[0], 'crash_count': r[1], 'resolved_count': r[2]} for r in rows])


@app.route('/api/package/<int:pkg_id>/versions')
def api_package_versions(pkg_id):
    conn, cursor = get_db()
    cursor.execute('''
        SELECT pi.version, COUNT(ci.id) as crash_count,
               SUM(CASE WHEN cs.resolved = 1 THEN 1 ELSE 0 END) as resolved_count
        FROM crash_info ci
        JOIN package_info pi ON ci.package_id = pi.id
        LEFT JOIN crash_stack cs ON ci.stack_id = cs.id
        WHERE pi.id = %s
        GROUP BY pi.version
        ORDER BY pi.version DESC
    ''', (pkg_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([{'version': r[0], 'crash_count': r[1], 'resolved_count': r[2]} for r in rows])


@app.route('/api/stats')
def api_stats():
    conn, cursor = get_db()
    cursor.execute('SELECT COUNT(*) FROM crash_info')
    total = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM crash_stack WHERE resolved = 0 AND same_stack IS NULL')
    unresolved = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM crash_stack WHERE resolved = 1 AND same_stack IS NULL')
    resolved = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM crash_stack WHERE resolved = 2 AND same_stack IS NULL')
    wontfix = cursor.fetchone()[0]
    cursor.execute('''
        SELECT pi.name, pi.version, COUNT(ci.id) as cnt
        FROM crash_info ci
        JOIN package_info pi ON ci.package_id = pi.id
        GROUP BY ci.package_id ORDER BY cnt DESC LIMIT 10
    ''')
    top = cursor.fetchall()
    cursor.close()
    conn.close()

    return jsonify({
        'total_crashes': total,
        'unresolved': unresolved,
        'resolved': resolved,
        'wontfix': wontfix,
        'top_packages': [{'name': r[0], 'version': r[1], 'count': r[2]} for r in top]
    })


@app.route('/api/stack/<int:stack_pk>/resolve', methods=['POST'])
def api_resolve_stack(stack_pk):
    data = request.get_json(force=True)
    resolved = data.get('resolved', 1)
    code_link = (data.get('code_link') or '').strip() or None
    reason = (data.get('reason') or '').strip() or None
    resolved_version = (data.get('resolved_version') or '').strip() or None
    conn = get_connection()
    conn.autocommit(False)
    cursor = conn.cursor()
    cursor.execute('USE coredump_db')
    try:
        cursor.execute(
            'UPDATE crash_stack SET resolved = %s, code_link = %s, reason = %s, resolved_version = %s WHERE id = %s',
            (resolved, code_link, reason, resolved_version, stack_pk)
        )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/stack/<int:stack_pk>/symbolicate', methods=['POST'])
def api_symbolicate_stack(stack_pk):
    data = request.get_json(force=True)
    symbolicated = (data.get('symbolicated_stack') or '').strip() or None
    conn = get_connection()
    conn.autocommit(False)
    cursor = conn.cursor()
    cursor.execute('USE coredump_db')
    try:
        cursor.execute(
            'UPDATE crash_stack SET symbolicated_stack = %s WHERE id = %s',
            (symbolicated, stack_pk)
        )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/stack/merge', methods=['POST'])
def api_merge_stack():
    data = request.get_json(force=True)
    id1 = data.get('id1')
    id2 = data.get('id2')
    if not id1 or not id2 or id1 == id2:
        return jsonify({'error': '请选择两个不同的堆栈'}), 400

    conn = get_connection()
    conn.autocommit(False)
    cursor = conn.cursor()
    cursor.execute('USE coredump_db')

    def follow_canonical(sid):
        visited = set()
        while sid and sid not in visited:
            visited.add(sid)
            cursor.execute('SELECT same_stack FROM crash_stack WHERE id = %s', (sid,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                sid = row[0]
            else:
                break
        return sid

    canonical1 = follow_canonical(id1)
    canonical2 = follow_canonical(id2)
    if canonical1 == canonical2:
        return jsonify({'error': '这两个堆栈已经合并'}), 400

    new_canonical = min(canonical1, canonical2)
    old_canonical = max(canonical1, canonical2)
    try:
        cursor.execute(
            'UPDATE crash_stack SET same_stack = %s WHERE same_stack = %s',
            (new_canonical, old_canonical)
        )
        cursor.execute(
            'UPDATE crash_stack SET same_stack = %s WHERE id = %s AND same_stack IS NULL',
            (new_canonical, old_canonical)
        )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/stack/<int:stack_pk>/versions')
def api_stack_versions(stack_pk):
    """该堆栈（含合并组）涉及的崩溃版本及次数，用于趋势弹窗的版本过滤下拉框"""
    conn, cursor = get_db()
    cursor.execute('''
        SELECT pi.version, COUNT(ci.id) as cnt
        FROM crash_info ci
        JOIN package_info pi ON ci.package_id = pi.id
        WHERE ci.stack_id IN (SELECT id FROM crash_stack WHERE id = %s OR same_stack = %s)
          AND pi.version IS NOT NULL
        GROUP BY pi.version
        ORDER BY cnt DESC
    ''', (stack_pk, stack_pk))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([{'version': r[0], 'count': r[1]} for r in rows])


@app.route('/api/stack/<int:stack_pk>/trend')
def api_stack_trend(stack_pk):
    days = request.args.get('days', 30, type=int)
    version = request.args.get('version', '').strip()
    ver_sql = ''
    params = [stack_pk, stack_pk, days]
    if version:
        ver_sql = ' AND pi.version = %s'
        params.append(version)
    conn, cursor = get_db()
    cursor.execute(f'''
        SELECT FROM_UNIXTIME(ci.crash_time, "%%Y-%%m-%%d") as day, COUNT(*) as cnt
        FROM crash_info ci
        LEFT JOIN package_info pi ON ci.package_id = pi.id
        WHERE ci.stack_id IN (SELECT id FROM crash_stack WHERE id = %s OR same_stack = %s)
          AND ci.crash_time >= UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL %s DAY))
          {ver_sql}
        GROUP BY day ORDER BY day
    ''', params)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify([{'date': r[0], 'count': r[1]} for r in rows])


@app.route('/api/crash/<int:crash_pk>/resolve', methods=['POST'])
def api_resolve_by_crash(crash_pk):
    data = request.get_json(force=True)
    resolved = data.get('resolved', 1)
    code_link = data.get('code_link', '').strip() or None
    conn = get_connection()
    conn.autocommit(False)
    cursor = conn.cursor()
    cursor.execute('USE coredump_db')
    try:
        cursor.execute('SELECT stack_id FROM crash_info WHERE id = %s', (crash_pk,))
        row = cursor.fetchone()
        if not row or not row[0]:
            return jsonify({'error': 'no stack linked'}), 404
        cursor.execute(
            'UPDATE crash_stack SET resolved = %s, code_link = %s WHERE id = %s',
            (resolved, code_link, row[0])
        )
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/import-logs')
def api_import_logs():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    offset = (page - 1) * per_page
    conn, cursor = get_db()
    cursor.execute('SELECT COUNT(*) FROM imported_file')
    total = cursor.fetchone()[0]
    cursor.execute(
        'SELECT id, file_name, file_rows, imported_rows, imported_at FROM imported_file ORDER BY file_name DESC LIMIT %s OFFSET %s',
        (per_page, offset)
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return jsonify({
        'total': total,
        'pages': (total + per_page - 1) // per_page,
        'data': [{'id': r[0], 'file_name': r[1], 'file_rows': r[2], 'imported_rows': r[3], 'imported_at': str(r[4])} for r in rows]
    })


@app.route('/api/import', methods=['POST'])
def api_import():
    data = request.get_json(force=True)
    csv_path = data.get('csv_path', '').strip()
    if not csv_path or not os.path.isfile(csv_path):
        return jsonify({'error': '文件不存在'}), 400
    try:
        from import_csv import import_csv
        result = import_csv(csv_path, update_existing=data.get('update_existing', False))
        return jsonify({'ok': True, 'imported': result['total'], 'skipped': result['skipped']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
