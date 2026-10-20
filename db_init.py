# -*- coding: utf-8 -*-
import os
import pymysql


def load_env(path=None):
    """读取脚本目录下的 .env（存在时），补齐未设置的环境变量。

    不覆盖已在环境中存在的变量，因此 export 的优先级更高。
    """
    env_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_env()


def get_connection():
    return pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', 3306)),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASSWORD', ''),
        charset='utf8mb4'
    )


def init_database():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("CREATE DATABASE IF NOT EXISTS coredump_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cursor.execute("USE coredump_db")

    # a. 包信息表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS package_info (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            version VARCHAR(255) NOT NULL,
            UNIQUE KEY uk_name_version (name, version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # b. 崩溃信息表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crash_info (
            id INT AUTO_INCREMENT PRIMARY KEY,
            crash_id VARCHAR(255) NOT NULL COMMENT '崩溃记录ID',
            sys_version VARCHAR(255) COMMENT '系统版本',
            crash_time INT UNSIGNED COMMENT '崩溃时间(UNIX秒)',
            package_id INT COMMENT '所属包ID',
            binary_id INT COMMENT '崩溃二进制ID',
            stack_id INT COMMENT '堆栈ID',
            process_id VARCHAR(64) COMMENT '进程ID',
            symbol_id INT COMMENT '符号ID',
            UNIQUE KEY uk_crash_id (crash_id),
            KEY idx_package_id (package_id),
            KEY idx_stack_id (stack_id),
            KEY idx_stack_time (stack_id, crash_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # c. 崩溃堆栈表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crash_stack (
            id INT AUTO_INCREMENT PRIMARY KEY,
            stack_md5 VARCHAR(64) NOT NULL COMMENT '堆栈内容MD5',
            stack_text LONGTEXT NOT NULL COMMENT '崩溃堆栈(已清理)',
            resolved TINYINT NOT NULL DEFAULT 0 COMMENT '解决状态: 0未解决 1已解决 2解不动',
            code_link VARCHAR(512) DEFAULT NULL COMMENT '修复代码链接',
            reason VARCHAR(1024) DEFAULT NULL COMMENT '解决原因',
            resolved_version VARCHAR(255) DEFAULT NULL COMMENT '解决版本',
            same_stack INT DEFAULT NULL COMMENT '合并到的目标堆栈ID',
            UNIQUE KEY uk_stack_md5 (stack_md5),
            KEY idx_resolved (resolved)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # d. 崩溃hash映射表（crash_hash → stack_id，避免重复MD5计算）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crash_hash (
            crash_hash VARCHAR(64) NOT NULL PRIMARY KEY,
            stack_id INT NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # e. 符号信息表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS symbol_info (
            id INT AUTO_INCREMENT PRIMARY KEY,
            buildid VARCHAR(255),
            KEY idx_buildid (buildid)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # f. 二进制信息表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS binary_info (
            id INT AUTO_INCREMENT PRIMARY KEY,
            package_id INT,
            binary_name VARCHAR(255),
            KEY idx_package_id (package_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS imported_file (
            id INT AUTO_INCREMENT PRIMARY KEY,
            file_name VARCHAR(512) NOT NULL UNIQUE,
            file_rows INT DEFAULT 0,
            imported_rows INT DEFAULT 0,
            imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    conn.commit()
    cursor.close()
    conn.close()
    print("数据库初始化完成: coredump_db")


if __name__ == '__main__':
    init_database()
