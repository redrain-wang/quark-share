"""
转存任务流程
mac_vod 今年的电影 → PanSou 搜索 → 夸克 API 转存 → 生成分享 → 入库

使用方法：
  python task_flow.py init      # 从 mac_vod 初始化任务列表
  python task_flow.py run       # 处理待转存任务（可反复执行直到全部完成）
  python task_flow.py status    # 查看任务状态统计
"""
import asyncio
import logging
import re
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiomysql
import pymysql

from config import DB_CONFIG, ONLINE_DB
from pansou_client import search_links
from quark_api import QuarkAPI, load_cookie_header, QuarkAPIError
import browser_share

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("task_flow")

COOKIE_FILE = Path(__file__).parent / "browser_data" / "quark_1_cookies.json"
FOLDER_NAME = "easysvip.com"

# 每部电影最多尝试的链接数（失效就换下一个）
MAX_LINK_TRY = 5
# 单次转存体积上限（GB），超过则优先换其他链接
MAX_TRANSFER_GB = 15.0
# 任务之间的间隔（秒），保守防风控（夸克 WAF 对高频请求会 401 拦截）
TASK_INTERVAL = (60, 120)
# 每条链接尝试之间的间隔（秒）
LINK_RETRY_INTERVAL = (15, 30)


# ================================================================
#  数据库辅助
# ================================================================

def _cjk(s: str) -> str:
    """提取字符串中的中文字符"""
    import re
    return "".join(re.findall(r"[\u4e00-\u9fff]", s or ""))


def _is_relevant(movie_name: str, note: str) -> bool:
    """
    判断搜索结果的描述是否与影片名相关（过滤 PanSou 的不相关结果）
    规则：影片名的中文部分（>=2字）出现在结果的中文提取里
    """
    target = _cjk(movie_name)
    if len(target) < 2:
        # 纯英文/数字片名，用原始名判断
        n = (movie_name or "").lower().replace(" ", "")
        t = (note or "").lower().replace(" ", "")
        return bool(n) and n in t
    hay = _cjk(note)
    return target in hay


def sync_db():
    """同步连接（用于初始化任务）"""
    return pymysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        database=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )


# ================================================================
#  初始化任务：从 mac_vod 提取今年的电影
# ================================================================

def init_tasks():
    """
    从 mac_vod 提取 2025/2026 年的影片，按名称去重，
    写入 quark_transfer，并在 quark_link_vod 中建立 vod_id 映射。
    """
    conn = sync_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT vod_name, MIN(vod_year) as yr, GROUP_CONCAT(vod_id) as ids
        FROM mac_vod
        WHERE vod_year IN ('2025', '2026')
        GROUP BY vod_name
    """)
    movies = cur.fetchall()
    logger.info(f"从 mac_vod 获取到 {len(movies)} 部影片（按名称去重）")

    inserted = 0
    for name, year, ids in movies:
        vod_ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]

        # 插入任务（忽略已存在的）
        cur.execute(
            "INSERT IGNORE INTO quark_transfer (vod_name, vod_year) VALUES (%s, %s)",
            (name, year),
        )
        if cur.rowcount == 0:
            continue
        inserted += 1

        cur.execute("SELECT id FROM quark_transfer WHERE vod_name = %s", (name,))
        transfer_id = cur.fetchone()[0]

        # 建立 vod_id 映射（一对多）
        for vid in vod_ids:
            cur.execute(
                "INSERT IGNORE INTO quark_link_vod (transfer_id, vod_id) VALUES (%s, %s)",
                (transfer_id, vid),
            )

    logger.info(f"新插入任务: {inserted} 条")
    conn.close()


# ================================================================
#  处理任务
# ================================================================

async def get_pending_tasks(limit: int = 100) -> list[dict]:
    """获取待处理任务（按热度降序，热门影片优先处理）"""
    conn = await aiomysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        db=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT id, vod_name, vod_year, try_count, quark_fid
                FROM quark_transfer
                WHERE (status IN (0, 5) AND (try_count < 5 OR quark_fid != '')) OR status = 2
                ORDER BY priority DESC, release_date DESC, vod_hits DESC, id ASC
                LIMIT %s
            """, (limit,))
            return await cur.fetchall()
    finally:
        conn.close()


async def update_task(transfer_id: int, **fields):
    """更新任务字段"""
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [transfer_id]
    conn = await aiomysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        db=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE quark_transfer SET {sets} WHERE id = %s", values
            )
    finally:
        conn.close()


async def _mark_success(task_id: int, name: str, my_url: str, my_pwd: str,
                        original_url: str = "", original_pwd: str = "", quark_fid: str = "",
                        try_count: int = 0):
    """标记任务成功：更新任务表 + 写入网站网盘表（每个 vod_id 一条，详情页自动展示）"""
    await update_task(
        task_id,
        status=3,
        try_count=try_count,
        original_url=original_url,
        original_pwd=original_pwd,
        quark_fid=quark_fid,
        my_url=my_url,
        my_pwd=my_pwd,
        error_msg="",
    )

    # 写入网站的 mac_vod_netdisk 表（线上结构：id/vod_id/type/share_url/pwd/quality/create_time）
    conn = await aiomysql.connect(
        host=DB_CONFIG["host"], port=DB_CONFIG["port"],
        user=DB_CONFIG["user"], password=DB_CONFIG["password"],
        db=DB_CONFIG["database"], charset="utf8mb4", autocommit=True,
    )
    try:
        async with conn.cursor() as cur:
            # 取该任务映射的所有 vod_id
            await cur.execute(
                "SELECT vod_id FROM quark_link_vod WHERE transfer_id = %s", (task_id,)
            )
            vod_ids = [r[0] for r in await cur.fetchall()]

            for vid in vod_ids:
                # 同一 vod_id + type 已有则跳过（防重复）
                await cur.execute(
                    "SELECT id FROM mac_vod_netdisk WHERE vod_id = %s AND type = 'quark'",
                    (vid,),
                )
                if await cur.fetchone():
                    continue
                await cur.execute(
                    "INSERT INTO mac_vod_netdisk (vod_id, type, share_url, pwd, quality) "
                    "VALUES (%s, 'quark', %s, %s, 3)",
                    (vid, my_url, my_pwd),
                )
        logger.info(f"✅ 任务[{task_id}] {name}: {my_url} → 已写入 {len(vod_ids)} 个 vod_id 的网盘资源")
    finally:
        conn.close()

    # 实时同步到线上库（194.41.36.57，幂等，失败不影响本地流程）
    try:
        await _sync_rows_to_online(vod_ids, my_url, my_pwd)
    except Exception as e:
        logger.warning(f"线上同步失败（sync_push.py 会补）: {e}")


async def _sync_rows_to_online(vod_ids: list[int], my_url: str, my_pwd: str):
    """将本次成功的新行直写线上库（幂等）"""
    import pymysql

    oconn = pymysql.connect(
        **{k: ONLINE_DB[k] for k in ("host", "port", "user", "password", "database")},
        charset="utf8mb4", connect_timeout=10, read_timeout=20, autocommit=True,
    )
    try:
        ocur = oconn.cursor()
        inserted = 0
        for vid in vod_ids:
            ocur.execute(
                "SELECT id FROM mac_vod_netdisk WHERE vod_id=%s AND share_url=%s LIMIT 1",
                (vid, my_url),
            )
            if ocur.fetchone():
                continue
            ocur.execute(
                "INSERT INTO mac_vod_netdisk (vod_id, type, share_url, pwd, quality, create_time) "
                "VALUES (%s, 'quark', %s, %s, 3, NOW())",
                (vid, my_url, my_pwd or ""),
            )
            inserted += 1
        if inserted:
            logger.info(f"☁️ 已实时同步 {inserted} 条到线上库")
    finally:
        oconn.close()


async def _estimate_share_size_gb(api: QuarkAPI, share_url: str, passcode: str = "") -> float | None:
    """
    预估分享内容的总体积（GB）。取分享顶层文件大小；若顶层是文件夹则下钻一层。
    失败返回 None（不阻塞流程）。
    """
    m = re.search(r"pan\.quark\.cn/s/([a-zA-Z0-9]+)", share_url)
    if not m:
        return None
    try:
        pwd_id = m.group(1)
        stoken = await api.get_stoken(pwd_id, passcode)

        async def _sum(pdir_fid: str, depth: int = 0) -> float:
            d = await api._request(
                "GET", "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail",
                params={"pr": "ucpro", "fr": "pc", "pwd_id": pwd_id, "stoken": stoken,
                        "pdir_fid": pdir_fid, "_page": "1", "_size": "200",
                        "_fetch_total": "0"},
            )
            total = 0.0
            for it in d.get("data", {}).get("list", []):
                if it.get("dir"):
                    if depth < 1:
                        total += await _sum(it["fid"], depth + 1)
                else:
                    total += (it.get("size", 0) or 0)
            return total

        total_bytes = await _sum("0")
        return total_bytes / 1024 ** 3
    except Exception:
        return None


async def _rank_links_by_size(api: QuarkAPI, links: list, max_gb: float = 15.0, probe: int = 6) -> list:
    """
    按预估体积给候选链接排序：小体积优先、4K 提示靠后、超限的跳过。
    体积未知的按原顺序排在中间。
    """
    scored = []
    for i, link in enumerate(links[:probe]):
        size = await _estimate_share_size_gb(api, link.url, link.password)
        note = link.note or ""
        hint_4k = any(k in note for k in ("4K", "2160", "UHD", "HDR", "蓝光", "原盘"))
        over = size is not None and size > max_gb
        scored.append({"link": link, "size": size, "over": over,
                       "hint4k": hint_4k, "idx": i})
        logger.info(f"  [选链] {link.url[-12:]} 预估 {('%.1fGB' % size) if size is not None else '未知'}"
                    f"{' ⚠️超限' if over else ''}{' [4K]' if hint_4k else ''}")
    scored.sort(key=lambda x: (
        x["over"], x["hint4k"],
        x["size"] if x["size"] is not None else 999, x["idx"],
    ))
    ok = [s["link"] for s in scored if not s["over"]]
    if not ok:  # 全超限时退回原列表（避免无片可转）
        ok = [s["link"] for s in scored]
    return ok


async def _create_share_via_browser(api: QuarkAPI, folder_fid: str, fids: list[str], name: str,
                                    account_id: int = 1) -> dict | None:
    """
    通过真 Chrome 的可信鼠标事件（UI 全流程）创建分享。
    fid → 网盘中文件的精确显示名（含混淆字符）→ 拟人点击 UI 创建。
    失败时尝试从分享列表恢复。
    """
    try:
        # fid 级去重：该文件已有存活分享 → 直接复用，绝不重复创建
        existing = await api.find_share_by_fid(fids)
        if existing:
            logger.info(f"[浏览器分享] fid 已有存活分享，直接复用: {existing['url']}")
            return existing
        # fid → 显示名
        files = await api.list_folder_files(folder_fid)
        fid_map = {f["fid"]: f["file_name"] for f in files}
        names = [fid_map[f] for f in fids if f in fid_map]
        if not names:
            logger.warning(f"[浏览器分享] 未找到 {fids} 对应的文件名")
            return None
        return await browser_share.create_share_via_trusted_ui(names, account_id=account_id)
    except Exception as e:
        logger.warning(f"[浏览器分享] 失败({e})，尝试从分享列表恢复...")
        await asyncio.sleep(2)
        import time as _t
        return (await api.find_new_share_since(int(_t.time() * 1000) - 20000)
                or await api.find_share_by_name(name))


async def process_task(api: QuarkAPI, folder_fid: str, task: dict, share_enabled: bool = True,
                       account_id: int = 1):
    """
    处理单个任务：
    -1. 已有转存 fid（quark_fid）→ 直接补分享，绝不重复转存
    0. 去重检查：网盘里已有同名文件 → 直接复用/补分享
    1. PanSou 搜索影片名
    2. 逐个尝试链接：转存 + 分享（分享走真 Chrome）
    3. 成功则更新数据库（status=3），全部失败则标记无资源/失败

    share_enabled=False 时只转存（浏览器不可用时兜底），任务标记 status=2
    """
    task_id = task["id"]
    name = task["vod_name"]
    year = task.get("vod_year", "")
    try_count = task.get("try_count", 0) + 1

    logger.info(f"─── 任务[{task_id}] {name} ({year}) 第{try_count}次尝试 ───")

    # -1. 已有转存 fid → 绝不重复转存
    saved_fid = (task.get("quark_fid") or "").split(",")[0] if task.get("quark_fid") else ""
    if saved_fid:
        # fid 指向的文件可能已被网盘清理，不存在则清掉走正常转存（防死循环）
        try:
            files = await api.list_folder_files(folder_fid)
            if not any(f["fid"] == saved_fid for f in files):
                logger.info(f"任务[{task_id}] {name}: fid 文件已不存在，清除 fid 走正常流程")
                await update_task(task_id, quark_fid="", status=0)
                return
        except Exception as e:
            logger.warning(f"任务[{task_id}] fid 校验异常: {e}")
    if saved_fid:
        if not share_enabled:
            # 只转存模式：文件已在网盘，无需任何操作，等配额恢复
            logger.info(f"任务[{task_id}] {name}: 已有转存 fid，本轮跳过（等分享配额）")
            return
        logger.info(f"任务[{task_id}] {name}: 已有转存 fid，直接补分享（真浏览器）")
        share = await _create_share_via_browser(api, folder_fid, [saved_fid], name, account_id)
        if share:
            await _mark_success(task_id, name, share["url"], share.get("passcode", ""),
                                try_count=try_count)
        else:
            await update_task(task_id, status=5, error_msg="补分享失败，稍后重试")
        return

    # 0. 防重复：网盘中已有同名文件？（名称模糊匹配兜底）
    try:
        existing = await api.find_existing_file(folder_fid, name)
    except Exception as e:
        logger.warning(f"任务[{task_id}] 去重检查异常: {e}")
        existing = None

    if existing:
        existing_name = existing["file_name"]
        logger.info(f"任务[{task_id}] {name}: 网盘已有 [{existing_name}]，跳过转存")
        # 已有分享吗？
        share = await api.find_share_by_name(name)
        if share:
            await _mark_success(task_id, name, share["url"], share.get("passcode", ""),
                                try_count=try_count)
            return
        # 没有分享 → 给已有文件补一个分享（真浏览器）
        share = await _create_share_via_browser(api, folder_fid, [existing["fid"]], existing_name, account_id)
        if share:
            await _mark_success(task_id, name, share["url"], share.get("passcode", ""),
                                try_count=try_count)
        else:
            await update_task(task_id, status=5,
                              error_msg="已有文件但分享失败，稍后重试")
        return

    # 1. 搜索
    result = await search_links(name, cloud_types=["quark"])
    if not result.links:
        await update_task(
            task_id, try_count=try_count,
            status=4 if try_count >= 3 else 0,  # 3次都没搜到 = 无资源
            error_msg="PanSou 无夸克资源",
        )
        logger.warning(f"任务[{task_id}] {name}: 无搜索结果")
        return

    logger.info(f"任务[{task_id}] {name}: 搜到 {len(result.links)} 条链接")

    # 体积感知选链：预估各候选体积，小体积优先、跳过超大合集（省网盘空间）
    try:
        result.links = await _rank_links_by_size(api, result.links, max_gb=MAX_TRANSFER_GB)
    except Exception as e:
        logger.warning(f"任务[{task_id}] 选链排序异常（按原顺序）: {e}")

    # 1.5 相关性过滤：剔除 PanSou 返回的不相关结果
    relevant = [l for l in result.links if _is_relevant(name, l.note)]
    dropped = len(result.links) - len(relevant)
    if dropped > 0:
        logger.info(f"任务[{task_id}] {name}: 过滤掉 {dropped} 条不相关结果，剩 {len(relevant)} 条")
    if not relevant:
        await update_task(
            task_id, try_count=try_count,
            status=4 if try_count >= 3 else 0,
            error_msg="搜索结果均与影片不相关",
        )
        logger.warning(f"任务[{task_id}] {name}: 无相关结果")
        return
    result.links = relevant

    # 2. 逐个尝试转存（转存走API，分享走真浏览器）
    for i, link in enumerate(result.links[:MAX_LINK_TRY]):
        if i > 0:
            # 每条链接之间稍作休息，降低 WAF 频率限制风险
            delay = random.uniform(*LINK_RETRY_INTERVAL)
            await asyncio.sleep(delay)
        try:
            r = await api.transfer_and_share(
                share_url=link.url,
                passcode=link.password,
                target_folder_fid=folder_fid,
                title=name,
                skip_share=True,  # 只转存，分享走真浏览器
            )
            saved_fids = r["saved_fids"]

            # 立即记录 fid，任何后续失败都能精确补分享、不重复转存
            await update_task(
                task_id,
                original_url=link.url, original_pwd=link.password,
                quark_fid=",".join(saved_fids[:3]),
            )

            if not share_enabled:
                await update_task(
                    task_id, status=2,
                    error_msg="已转存，等待浏览器分享",
                )
                logger.info(f"📥 任务[{task_id}] {name}: 已转存（浏览器不可用，待补分享）")
                return

            # 分享环节（真 Chrome）
            share = await _create_share_via_browser(api, folder_fid, saved_fids, name, account_id)
            if share:
                await _mark_success(
                    task_id, name, share["url"], share.get("passcode", ""),
                    try_count=try_count,
                    original_url=link.url, original_pwd=link.password,
                    quark_fid=",".join(saved_fids[:3]),
                )
                return

            # 浏览器分享失败 → 已转存，下轮补分享，不消耗重试次数
            await update_task(
                task_id, status=5,
                error_msg="已转存，分享待重试",
            )
            logger.warning(f"任务[{task_id}] {name}: 已转存但分享失败 → 下轮补分享")
            return

        except QuarkAPIError as e:
            # 转存阶段错误（链接无效/空间不足等）→ 换下一个链接
            logger.warning(f"任务[{task_id}] {name}: 链接{i+1}失败({e}) → 换下一个")
            continue
        except Exception as e:
            logger.warning(f"任务[{task_id}] {name}: 链接{i+1}异常({e}) → 换下一个")
            continue

    # 所有链接都失败
    await update_task(
        task_id, try_count=try_count,
        status=5,
        error_msg=f"前{MAX_LINK_TRY}条链接均转存失败",
    )
    logger.error(f"任务[{task_id}] {name}: 所有链接转存失败")


async def audit_and_reset_completed():
    """
    审计巡检：匿名验证所有已完成任务的分享是否存活。
    被夸克审计拦截的（分享不存在）→ 清除线上死链接行 + 重置任务回队列。
    """
    import aiohttp

    conn = pymysql.connect(**{k: DB_CONFIG[k] for k in ("host", "port", "user", "password", "database")},
                           charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT id, vod_name, my_url FROM quark_transfer WHERE status=3 AND my_url != ''")
    tasks = cur.fetchall()
    conn.close()

    if not tasks:
        return

    dead_ids = []
    async with aiohttp.ClientSession() as session:
        for tid, name, url in tasks:
            m = re.search(r"/s/([a-zA-Z0-9]+)", url)
            if not m:
                continue
            try:
                async with session.post(
                    "https://drive-h.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc",
                    json={"pwd_id": m.group(1), "passcode": "", "support_visit_limit_private_share": True},
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pan.quark.cn/"},
                ) as resp:
                    d = await resp.json()
                    if d.get("code") != 0:
                        dead_ids.append(tid)
            except Exception:
                continue

    if not dead_ids:
        logger.info(f"审计巡检: {len(tasks)} 条分享全部存活")
        return 0

    conn = pymysql.connect(**{k: DB_CONFIG[k] for k in ("host", "port", "user", "password", "database")},
                           charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    oconn = pymysql.connect(
        **{k: ONLINE_DB[k] for k in ("host", "port", "user", "password", "database")},
        charset="utf8mb4", connect_timeout=10, read_timeout=15, autocommit=True,
    )
    ocur = oconn.cursor()
    for tid in dead_ids:
        cur.execute("SELECT my_url FROM quark_transfer WHERE id=%s", (tid,))
        row = cur.fetchone()
        if row and row[0]:
            try:
                ocur.execute("DELETE FROM mac_vod_netdisk WHERE share_url=%s", (row[0],))
            except Exception:
                pass
    oconn.close()
    cur.execute(
        f"UPDATE quark_transfer SET status=0, my_url='', my_pwd='', "
        f"error_msg='分享被审计拦截，已重置' WHERE id IN ({','.join(map(str, dead_ids))})"
    )
    conn.close()
    logger.warning(f"审计巡检: {len(dead_ids)}/{len(tasks)} 条分享被拦截，已清除线上死链并重置回队列")
    return len(dead_ids)



# ================================================================
#  主流程
# ================================================================

async def run(limit: int = 100, share_enabled: bool = True, account_id: int = 1):
    """执行一轮任务。account_id 指定使用哪个网盘账号（独立 Cookie / Chrome / 文件夹）"""
    from config import get_account_profile
    cookie_file = get_account_profile(account_id)["cookie_file"]
    cookie = load_cookie_header(cookie_file)

    boot = QuarkAPI(cookie)
    try:
        if not await boot.check_login():
            logger.error(f"账号{account_id} Cookie 已失效，请重新登录！")
            return
        folder_fid = await boot.get_or_create_folder(FOLDER_NAME)
    finally:
        await boot.close()
    logger.info(f"[账号{account_id}] 转存目标文件夹: {FOLDER_NAME} (fid={folder_fid})")

    tasks = await get_pending_tasks(limit)
    logger.info(f"待处理任务: {len(tasks)} 条")

    done = 0
    for task in tasks:
        # 每个任务使用全新会话（手动验证：新会话的成功率远高于长会话）
        api = QuarkAPI(cookie)
        try:
            await process_task(api, folder_fid, task, share_enabled=share_enabled, account_id=account_id)
        except Exception as e:
            logger.error(f"任务处理异常: {e}")
        finally:
            await api.close()
        done += 1

        # 任务之间随机休息（防风控）
        if done < len(tasks):
            delay = random.uniform(*TASK_INTERVAL)
            logger.info(f"休息 {delay:.0f}s 后继续...")
            await asyncio.sleep(delay)

    logger.info(f"本轮完成: 处理 {done} 条任务")


def status():
    """查看任务状态统计"""
    conn = sync_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT status, COUNT(*) FROM quark_transfer GROUP BY status
    """)
    labels = {0: "待处理", 1: "已找到资源", 2: "已转存", 3: "已完成✅", 4: "无资源", 5: "失败"}
    print("\n任务状态统计:")
    for s, c in cur.fetchall():
        print(f"  {labels.get(s, s)}: {c}")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "init":
        init_tasks()
    elif cmd == "run":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 100
        asyncio.run(run(limit))
    elif cmd == "status":
        status()
    else:
        print("用法: python task_flow.py [init|run|status]")
