"""
常驻批量运行器（v2 - 分享配额感知）

策略：
- 每轮开始先做"分享探针"：用已转存未分享的文件试建一个分享
  - 探针成功 → 正常模式（转存+分享），且探针本身完成了一条积压任务
  - 探针失败（配额受限）→ 只转存模式，任务标记 status=2，等配额恢复后补分享
- 轮间长冷却

用法：nohup python batch_runner.py > runner.log 2>&1 &
"""
import asyncio
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from quark_api import QuarkAPI, load_cookie_header, QuarkAPIError
from task_flow import get_pending_tasks, run, update_task, audit_and_reset_completed
from config import DB_CONFIG
import aiomysql
import browser_share
import daily_report
import public_page
import push_site_page
import discover_new
import traffic_report
import backup_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(Path(__file__).parent / "runner.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("runner")

TASKS_PER_ROUND = 10
ROUND_COOLDOWN = (1800, 2700)   # 30-45 分钟（悠着点，防风控）
IDLE_COOLDOWN = 3600

FOLDER_FID = "1cba61a854d847ddaffe74db44a9fd24"  # 账号1历史文件夹
FOLDER_NAME = "easysvip.com"
COOKIE_FILE = Path(__file__).parent / "browser_data" / "quark_1_cookies.json"


async def get_active_accounts() -> list[dict]:
    """取可用的夸克账号"""
    conn = await aiomysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        db=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id, phone FROM cloud_accounts WHERE disk_type='quark' AND status=1 ORDER BY id")
            return await cur.fetchall()
    finally:
        conn.close()


async def account_free_space_gb(account_id: int) -> float | None:
    """查账号剩余空间（GB）；Cookie 失效返回 None"""
    from config import get_account_profile
    try:
        cookie = load_cookie_header(get_account_profile(account_id)["cookie_file"])
        api = QuarkAPI(cookie)
        try:
            d = (await api._request("GET", "https://drive-member-h.quark.cn/1/clouddrive/member",
                                    params={"pr": "ucpro", "fr": "pc"})).get("data", {})
            use = d.get("use_capacity", 0) or 0
            total = d.get("total_capacity", 1) or 1
            return (total - use) / 1024 ** 3
        finally:
            await api.close()
    except Exception:
        return None


async def probe_and_complete_one(cookie, account_id: int = 1) -> bool | None:
    """
    分享探针：取一条"已转存未分享"的任务（status 2/5，有 quark_fid），试建分享。
    成功 → 完成该任务入库（status=3 + 写 mac_vod_netdisk），返回 True
    失败 → 配额受限，返回 False
    没有可探针的任务 → 返回 None（新环境，走正常模式）
    """
    conn = await aiomysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        db=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT id, vod_name, quark_fid FROM quark_transfer
                WHERE status IN (2, 5) AND quark_fid != ''
                ORDER BY vod_hits DESC LIMIT 1
            """)
            task = await cur.fetchone()
    finally:
        conn.close()

    if not task:
        return None

    fid = task["quark_fid"].split(",")[0]
    api = QuarkAPI(cookie)
    try:
        # fid → 网盘中的精确显示名（按账号解析其转存文件夹）
        folder_fid = await api.get_or_create_folder(FOLDER_NAME)
        files = await api.list_folder_files(folder_fid)
        name = next((f["file_name"] for f in files if f["fid"] == fid), None)
        if not name:
            logger.warning(f"探针: fid {fid[:12]} 未找到文件名")
            return False
        # 分享探针走真 Chrome 可信UI（WAF 只放行真实输入管线事件）
        try:
            share = await browser_share.create_share_via_trusted_ui([name], account_id=account_id)
        except Exception as e:
            logger.info(f"探针失败({str(e)[:60]}) → 本轮只转存")
            return False
    finally:
        await api.close()

    # 探针成功 → 完成该任务
    logger.info(f"🔎 探针成功，完成积压任务[{task['id']}] {task['vod_name'][:25]}: {share['url']}")
    conn = await aiomysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        db=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE quark_transfer SET status=3, my_url=%s, my_pwd=%s, error_msg='' WHERE id=%s",
                (share["url"], share.get("passcode", ""), task["id"]),
            )
            await cur.execute(
                "SELECT vod_id FROM quark_link_vod WHERE transfer_id=%s", (task["id"],)
            )
            vod_ids = [r[0] for r in await cur.fetchall()]
            for vid in vod_ids:
                await cur.execute(
                    "SELECT id FROM mac_vod_netdisk WHERE vod_id=%s AND type='quark' AND share_url=%s",
                    (vid, share["url"]),
                )
                if await cur.fetchone():
                    continue
                await cur.execute(
                    "INSERT INTO mac_vod_netdisk (vod_id, type, share_url, pwd, quality) "
                    "VALUES (%s, 'quark', %s, %s, 3)",
                    (vid, share["url"], share.get("passcode", "")),
                )
    finally:
        conn.close()
    return True


async def main():
    logger.info("=" * 50)
    logger.info("常驻批量运行器启动（v2 配额感知）")
    logger.info("=" * 50)

    cookie = load_cookie_header(COOKIE_FILE)
    round_no = 0
    while True:
        round_no += 1
        try:
            # 审计巡检（每轮检查已完成分享是否被夸克拦截）
            audit_dead = 0
            try:
                audit_dead = await audit_and_reset_completed()
            except Exception as e:
                logger.warning(f"审计巡检异常: {e}")

            # 分享探针（用账号1探测分享能力；账号1空间不足不影响分享测试）
            probe = await probe_and_complete_one(cookie, account_id=1)
            share_enabled = probe is not False  # False = 配额受限
            if probe is True:
                logger.info(f"[第{round_no}轮] 分享配额正常（探针完成1条积压）")
            elif probe is False:
                logger.info(f"[第{round_no}轮] 分享配额受限 → 本轮只转存不分享")
            else:
                logger.info(f"[第{round_no}轮] 无积压任务，正常模式")

            tasks = await get_pending_tasks(limit=TASKS_PER_ROUND)
            if not tasks:
                logger.info(f"[第{round_no}轮] 没有待处理任务，{IDLE_COOLDOWN}s 后再查")
                await asyncio.sleep(IDLE_COOLDOWN)
                continue

            # 账号选择：优先剩余空间大的可用账号（空间不足的跳过）
            accounts = await get_active_accounts()
            usable = []
            for acc in accounts:
                free = await account_free_space_gb(acc["id"])
                if free is None:
                    logger.warning(f"[账号{acc['id']}] Cookie 失效或无响应，跳过")
                    continue
                logger.info(f"[账号{acc['id']}] 剩余空间 {free:.1f}GB")
                if free > 5:  # 留 5GB 余量
                    usable.append((free, acc["id"]))
            usable.sort(reverse=True)
            if not usable:
                logger.error("❌ 所有账号空间不足或不可用，暂停本轮（等待扩容/清理）")
                await asyncio.sleep(1800)
                continue

            # 每轮处理：优先用剩余空间最大的账号
            account_id = usable[0][1]
            if len(usable) > 1:
                logger.info(f"可用账号空间: {[(a, round(f,1)) for f, a in usable]} → 选用账号{account_id}")

            logger.info(f"[第{round_no}轮] 处理 {len(tasks)} 条任务 (账号{account_id}, share_enabled={share_enabled})")
            await run(limit=TASKS_PER_ROUND, share_enabled=share_enabled, account_id=account_id)

            remaining = await get_pending_tasks(limit=100000)
            logger.info(f"[第{round_no}轮] 完成，剩余 {len(remaining)} 条")

            # 每日任务：新片发现 / 流量报告 / 数据库备份（各脚本自带日期去重）
            for daily_fn, label in ((discover_new.main, "新片发现"),
                                    (traffic_report.main, "流量报告"),
                                    (backup_db.main, "数据库备份")):
                try:
                    await asyncio.to_thread(daily_fn)
                except Exception as e:
                    logger.warning(f"{label}失败: {e}")

            # 每轮结束更新转存报告 + 公开汇总页
            try:
                await asyncio.to_thread(daily_report.main)
            except Exception as e:
                logger.warning(f"日报生成失败: {e}")
            try:
                await asyncio.to_thread(public_page.main)
            except Exception as e:
                logger.warning(f"公开页生成失败: {e}")
            try:
                await asyncio.to_thread(push_site_page.main)
            except Exception as e:
                logger.warning(f"网站页部署失败: {e}")

            # 自适应冷却：审计发现被拦分享 → 冷却加倍（降速保护账号）
            cd = random.randint(*ROUND_COOLDOWN)
            if audit_dead > 0:
                cd = min(cd * (1 + audit_dead), 7200)
                logger.warning(f"⚡ 自适应降速: 发现 {audit_dead} 条被拦分享，冷却延长至 {cd}s")
            logger.info(f"冷却 {cd}s 后进入下一轮...")
            await asyncio.sleep(cd)

        except Exception as e:
            logger.error(f"[第{round_no}轮] 异常: {e}")
            await asyncio.sleep(600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("运行器已停止")
