"""
对账脚本：找回"分享实际已创建但接口报 401"的任务

原理：分享列表条目带 first_fid（被分享文件的 fid），
与任务转存保存的文件 fid 精确匹配 → 找回真实链接

用法：python reconcile.py [--apply]
  预览模式默认；--apply 执行入库
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from quark_api import QuarkAPI, load_cookie_header
from config import DB_CONFIG
import aiomysql
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("reconcile")


def normalize(s: str) -> str:
    import re
    s = re.sub(r"\(\d+\)", "", s or "")
    s = re.sub(r"[\s（）()·：:【】\[\]（）]", "", s)
    return s.lower()


async def main(apply: bool):
    cookie = load_cookie_header("browser_data/quark_1_cookies.json")
    api = QuarkAPI(cookie)
    conn = await aiomysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        db=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )
    try:
        # 1. 建立分享索引（fid → 分享；归一化标题 → 分享）
        shares = await api.list_my_shares()
        share_by_fid = {}
        share_by_title = {}
        for s in shares:
            if s.get("status") != 1:
                continue
            info = {
                "url": f"https://pan.quark.cn/s/{s.get('share_id')}",
                "pwd": s.get("passcode", "") or "",
                "title": s.get("title", ""),
                "created_at": s.get("created_at", 0),
            }
            ff = s.get("first_fid")
            if ff:
                share_by_fid[ff] = info
            norm = normalize(s.get("title", ""))
            if norm:
                share_by_title[norm] = info
        logger.info(f"活跃分享 {len(share_by_fid)} 个")

        # 2. 取卡住的任务（已转存待分享，含 fid 为空的）
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT id, vod_name, quark_fid, my_url FROM quark_transfer
                WHERE status = 5
            """)
            stuck = await cur.fetchall()
        logger.info(f"卡住任务: {len(stuck)} 条")

        # 3. 对账：先按 fid 精确匹配，再按标题模糊匹配
        recovered = 0
        for t in stuck:
            found = None
            # 策略1：fid 精确
            for f in [x for x in (t["quark_fid"] or "").split(",") if x]:
                if f in share_by_fid:
                    found = share_by_fid[f]
                    break
            # 策略2：任务名 → 归一化标题匹配
            if not found:
                norm_name = normalize(t["vod_name"])
                if norm_name:
                    for title, info in share_by_title.items():
                        if norm_name in title or title in norm_name:
                            found = info
                            break

            if not found:
                continue

            logger.info(f"✅ 对账: 任务[{t['id']}] {t['vod_name'][:25]} → {found['url']}")
            if not apply:
                continue

            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE quark_transfer SET status=3, my_url=%s, my_pwd=%s, error_msg='' WHERE id=%s",
                    (found["url"], found["pwd"], t["id"]),
                )
                await cur.execute(
                    "SELECT vod_id FROM quark_link_vod WHERE transfer_id=%s", (t["id"],)
                )
                vod_ids = [r[0] for r in await cur.fetchall()]
                for vid in vod_ids:
                    await cur.execute(
                        "SELECT id FROM mac_vod_netdisk WHERE vod_id=%s AND type='quark' AND share_url=%s",
                        (vid, found["url"]),
                    )
                    if await cur.fetchone():
                        continue
                    await cur.execute(
                        "INSERT INTO mac_vod_netdisk (vod_id, type, share_url, pwd, quality) "
                        "VALUES (%s, 'quark', %s, %s, 3)",
                        (vid, found["url"], found["pwd"]),
                    )
            recovered += 1

        logger.info(f"{'已入库' if apply else '预览'}: 可找回 {recovered} 条")
    finally:
        await api.close()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
