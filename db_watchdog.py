"""
数据库看门狗：等待 MySQL 恢复后自动启动批量运行器
用法：nohup python db_watchdog.py > watchdog.log 2>&1 &
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pymysql
from config import DB_CONFIG


def db_ok() -> bool:
    try:
        conn = pymysql.connect(
            host=DB_CONFIG["host"], port=DB_CONFIG["port"],
            user=DB_CONFIG["user"], password=DB_CONFIG["password"],
            database=DB_CONFIG["database"], charset="utf8mb4",
            connect_timeout=5, read_timeout=10,
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


def main():
    print(f"[{time.strftime('%H:%M:%S')}] 看门狗启动，监控 MySQL {DB_CONFIG['host']}...", flush=True)
    while not db_ok():
        print(f"[{time.strftime('%H:%M:%S')}] 数据库无响应，60秒后重试...", flush=True)
        time.sleep(60)

    print(f"[{time.strftime('%H:%M:%S')}] ✅ 数据库已恢复！启动批量运行器...", flush=True)
    subprocess.Popen(
        ["python3", str(Path(__file__).parent / "batch_runner.py")],
        cwd=str(Path(__file__).parent),
        stdout=open(Path(__file__).parent / "runner.log", "w"),
        stderr=subprocess.STDOUT,
    )
    print(f"[{time.strftime('%H:%M:%S')}] 运行器已启动", flush=True)


if __name__ == "__main__":
    main()
