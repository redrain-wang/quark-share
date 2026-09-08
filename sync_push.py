"""
本地 → 线上 mac_vod_netdisk 增量同步（直写线上库）

线上库: 194.41.36.57 (db_online.ini)
本地库: 127.0.0.1 Docker (config.DB_CONFIG)

幂等：按 (vod_id, share_url) 判断线上是否已存在，不存在才插入
用法：
  python sync_push.py            # 推送一次增量
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pymysql
import config
from config import DB_CONFIG

# 线上库连接（从 config.ONLINE_DB 读取，db_online.ini 提供值）
ONLINE_DB = {**config.ONLINE_DB, "connect_timeout": 10, "read_timeout": 20}
STATE_FILE = Path(__file__).parent / ".sync_state"


def local_conn():
    return pymysql.connect(
        **{k: DB_CONFIG[k] for k in ("host", "port", "user", "password", "database")},
        charset="utf8mb4", autocommit=True, read_timeout=15,
    )


def online_conn():
    return pymysql.connect(**ONLINE_DB, charset="utf8mb4", autocommit=True)


def get_last_pushed_id() -> int:
    if STATE_FILE.exists():
        return int(STATE_FILE.read_text().strip() or 0)
    # 初始基准：本地表中用户老数据（dump 带来的）最大 id
    conn = local_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(id) FROM mac_vod_netdisk WHERE id <= 1000")
    base = cur.fetchone()[0] or 0
    conn.close()
    STATE_FILE.write_text(str(base))
    return base


def main():
    last_id = get_last_pushed_id()

    lconn = local_conn()
    lcur = lconn.cursor()
    lcur.execute(
        "SELECT id, vod_id, type, share_url, pwd, quality FROM mac_vod_netdisk "
        "WHERE id > %s ORDER BY id", (last_id,)
    )
    rows = lcur.fetchall()
    lconn.close()

    if not rows:
        print("无增量")
        return

    print(f"待同步 {len(rows)} 条 (本地 id > {last_id})")

    oconn = online_conn()
    ocur = oconn.cursor()
    inserted = skipped = 0
    max_id = last_id
    for lid, vod_id, typ, url, pwd, q in rows:
        max_id = lid
        ocur.execute(
            "SELECT id FROM mac_vod_netdisk WHERE vod_id = %s AND share_url = %s LIMIT 1",
            (vod_id, url),
        )
        if ocur.fetchone():
            skipped += 1
            continue
        ocur.execute(
            "INSERT INTO mac_vod_netdisk (vod_id, type, share_url, pwd, quality, create_time) "
            "VALUES (%s, %s, %s, %s, %s, NOW())",
            (vod_id, typ, url, pwd or "", q or 3),
        )
        inserted += 1
        print(f"  + vod_id={vod_id} {url[:55]}")
    oconn.close()

    STATE_FILE.write_text(str(max_id))
    print(f"✅ 同步完成: 新增 {inserted}, 跳过 {skipped}, 游标推进到本地 id={max_id}")


if __name__ == "__main__":
    main()
