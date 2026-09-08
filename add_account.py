"""
账号管理工具 - 添加/查看网盘账号

用法：
  python add_account.py add <quark|baidu> <手机号> <密码>
  python add_account.py list
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pymysql
from config import DB_CONFIG


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python add_account.py add <quark|baidu> <手机号> <密码>")
        print("  python add_account.py list")
        return

    action = sys.argv[1]
    conn = pymysql.connect(
        **{k: DB_CONFIG[k] for k in ("host", "port", "user", "password", "database")},
        charset="utf8mb4", autocommit=True,
    )
    cur = conn.cursor()

    if action == "add":
        if len(sys.argv) < 5:
            print("参数不足: python add_account.py add <quark|baidu> <手机号> <密码>")
            return
        disk_type, phone, password = sys.argv[2], sys.argv[3], sys.argv[4]
        cur.execute(
            "INSERT INTO cloud_accounts (disk_type, phone, password) VALUES (%s, %s, %s)",
            (disk_type, phone, password),
        )
        print(f"账号添加成功! ID: {cur.lastrowid} | {disk_type} | {phone}")

    elif action == "list":
        for dt in ("quark", "baidu"):
            cur.execute(
                "SELECT id, phone, status, last_used FROM cloud_accounts "
                "WHERE disk_type=%s ORDER BY id", (dt,)
            )
            rows = cur.fetchall()
            print(f"\n{dt.upper()} 账号:")
            for r in rows:
                print(f"  [{r[0]}] {r[1]} | {'正常' if r[2]==1 else '封禁'} | 最后使用: {r[3] or '从未'}")

    else:
        print(f"未知操作: {action}")

    conn.close()


if __name__ == "__main__":
    main()
