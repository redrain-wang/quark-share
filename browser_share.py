"""
浏览器分享模块 v3 (生产版) - 可信事件 UI 自动化

原理：夸克 WAF（baxia/fireyejs）会拦截一切程序特征创建的分享：
  - 裸 API fetch（aiohttp TLS 指纹）→ 审计拦截
  - 页面上下文 fetch → 审计拦截
  - JS el.click()（isTrusted=false）→ 审计拦截
唯一存活路径：真实输入管线事件（isTrusted=true）+ 拟人鼠标轨迹
驱动完整 UI 流程：选中文件 → 分享 → 创建分享

用法：
  await create_share_via_trusted_ui(["蝴蝶.mp4", ...])  # 传入网盘中文件的精确显示名
"""
import asyncio
import logging
import random
import re
import subprocess
import time
from pathlib import Path

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9223"
CDP_PROFILE_DIR = "/tmp/chrome_cdp_profile"
DRIVE_URL = "https://pan.quark.cn/list#/list/all"
TARGET_FOLDER = "easysvip.com"


class BrowserShareError(Exception):
    pass


def is_cdp_alive(timeout: float = 3) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_cdp_chrome():
    """确保 CDP Chrome 实例运行（不存在则复制登录配置启动）"""
    if is_cdp_alive():
        return True
    logger.info("[BrowserShare] CDP Chrome 未运行，启动中...")
    profile = Path(CDP_PROFILE_DIR)
    profile.mkdir(parents=True, exist_ok=True)
    if not (profile / "Default").exists():
        src = Path.home() / "Library/Application Support/Google/Chrome"
        try:
            subprocess.run(["cp", "-r", str(src / "Default"), str(profile / "Default")],
                           check=True, timeout=120)
            subprocess.run(["cp", str(src / "Local State"), str(profile / "Local State")],
                           check=True, timeout=30)
        except Exception as e:
            logger.warning(f"[BrowserShare] 复制配置失败: {e}")
    subprocess.Popen(
        ["open", "-na", "Google Chrome", "--args",
         f"--user-data-dir={CDP_PROFILE_DIR}",
         "--remote-debugging-port=9223", "--no-first-run"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(15):
        time.sleep(2)
        if is_cdp_alive():
            logger.info("[BrowserShare] CDP Chrome 就绪")
            return True
    raise BrowserShareError("CDP Chrome 启动超时")


# ====================================================================
#  拟人交互
# ====================================================================

async def _human_click(page, x: float, y: float):
    """拟人化鼠标点击：贝塞尔轨迹 + 抖动 + 按下延迟"""
    cx = x - random.uniform(120, 260)
    cy = y + random.uniform(-90, 90)
    await page.mouse.move(cx, cy)
    steps = random.randint(5, 8)
    for i in range(1, steps + 1):
        ix = cx + (x - cx) * i / steps + random.uniform(-2.5, 2.5)
        iy = cy + (y - cy) * i / steps + random.uniform(-2.5, 2.5)
        await page.mouse.move(ix, iy)
        await asyncio.sleep(random.uniform(0.015, 0.055))
    await page.mouse.move(x, y)
    await asyncio.sleep(random.uniform(0.08, 0.22))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.03, 0.09))
    await page.mouse.up()


async def _scroll_table(page):
    await page.evaluate(
        "() => { const t = document.querySelector('.ant-table-body'); if (t) t.scrollTop += 420; }"
    )


# ====================================================================
#  页面元素定位
# ====================================================================

async def _find_row_box(page, name: str, want: str = "checkbox"):
    """
    找文件行内元素的坐标。
    want: 'checkbox' (.ant-checkbox，用于选中) / 'name' (.filename-text，用于进入文件夹)
    """
    return await page.evaluate(
        """
        (args) => {
            const cells = document.querySelectorAll('.filename-text');
            for (const c of cells) {
                if ((c.textContent || '').trim() !== args.name) continue;
                const row = c.closest('tr') || c.closest('[class*="row"]');
                if (!row) continue;
                let el;
                if (args.want === 'checkbox') {
                    el = row.querySelector('.ant-checkbox') || row.querySelector('[class*="checkbox"]');
                } else {
                    el = c;
                }
                if (!el) continue;
                const r = el.getBoundingClientRect();
                return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                        inView: r.y > 115 && r.y < 905};
            }
            return null;
        }
        """,
        {"name": name, "want": want},
    )


def _exact_row(page, name: str):
    """构造精确匹配文件名的行定位器"""
    name_loc = page.locator(".filename-text").filter(
        has_text=re.compile(f"^{re.escape(name)}$")
    )
    return page.locator(".ant-table-row").filter(has=name_loc).first


async def _click_row_checkbox(page, file_name: str, max_tries: int = 30) -> bool:
    """定位器选中文件行内的复选框（自动滚动进视野+可信点击+勾选验证）"""
    row = _exact_row(page, file_name)
    cb = row.locator(".ant-checkbox").first
    for _ in range(max_tries):
        try:
            await cb.scroll_into_view_if_needed(timeout=2500)
            await cb.click(timeout=3000)
            await asyncio.sleep(0.8)
            checked = await page.evaluate(
                "() => document.querySelectorAll('input[type=\"checkbox\"]:checked').length"
            )
            if checked > 0:
                return True
            await _scroll_table(page)
            await asyncio.sleep(0.6)
        except Exception:
            await _scroll_table(page)
            await asyncio.sleep(0.6)
    return False


async def _enter_folder(page, folder_name: str, max_tries: int = 20) -> bool:
    """进入指定文件夹（点击文件名，自动滚动）"""
    name_el = (
        page.locator(".filename-text")
        .filter(has_text=re.compile(f"^{re.escape(folder_name)}$"))
        .first
    )
    for _ in range(max_tries):
        try:
            await name_el.scroll_into_view_if_needed(timeout=2500)
            await name_el.click(timeout=3000)
            await asyncio.sleep(3)
            return True
        except Exception:
            await _scroll_table(page)
            await asyncio.sleep(0.6)
    return False


async def _click_button(page, texts: list[str], timeout_s: float = 10) -> bool:
    """拟人点击文本匹配的可见按钮"""
    start = time.time()
    while time.time() - start < timeout_s:
        box = await page.evaluate(
            """
            (names) => {
                const els = document.querySelectorAll('button, span, [role="button"]');
                for (const el of els) {
                    const t = (el.textContent || '').trim();
                    if (names.includes(t) && el.offsetWidth > 0) {
                        const r = el.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2};
                    }
                }
                return null;
            }
            """,
            texts,
        )
        if box:
            await _human_click(page, box["x"], box["y"])
            return True
        await asyncio.sleep(0.8)
    return False


async def _read_share_link(page, timeout_s: float = 12):
    start = time.time()
    while time.time() - start < timeout_s:
        link = await page.evaluate(
            """
            () => {
                for (const i of document.querySelectorAll('input')) {
                    const v = (i.value || '').trim();
                    if (v.includes('pan.quark.cn/s/')) return v;
                }
                const dlg = document.querySelector('[class*="modal"], [class*="dialog"]');
                if (dlg) {
                    const m = (dlg.textContent || '').match(/https:\\/\\/pan\\.quark\\.cn\\/s\\/[a-zA-Z0-9]+/);
                    if (m) return m[0];
                }
                return null;
            }
            """
        )
        if link:
            return link
        await asyncio.sleep(1)
    return None


# ====================================================================
#  主入口
# ====================================================================

async def create_share_via_trusted_ui(file_names: list[str]) -> dict:
    """
    通过可信鼠标事件驱动夸克 UI，为指定文件（可多个，一次分享）创建链接。

    Args:
        file_names: 文件在网盘中的精确显示名（含混淆字符，通过 API 列表获得）

    Returns:
        {"url": "https://pan.quark.cn/s/xxx", "passcode": ""}
    """
    if isinstance(file_names, str):  # 兼容直接传单个文件名
        file_names = [file_names]
    if not file_names:
        raise BrowserShareError("文件名列表为空")

    ensure_cdp_chrome()
    pw = await async_playwright().start()
    start = time.time()

    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]

        # 每次全新打开页面，避免残留选中/弹窗状态
        page = await context.new_page()
        await page.goto(DRIVE_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 1. 进入 easysvip.com 文件夹（点击文件名）
        ok = await _enter_folder(page, TARGET_FOLDER)
        if not ok:
            raise BrowserShareError("未找到并进入 easysvip.com 文件夹")
        await asyncio.sleep(3)

        # 2. 逐个勾选目标文件（复选框，勾选后验证，最多重试2次）
        target_checked = 0
        for name in file_names:
            done = False
            for attempt in range(2):
                before = await page.evaluate(
                    "() => document.querySelectorAll('input[type=\"checkbox\"]:checked').length"
                )
                done = False
                for attempt in range(2):
                    before = await page.evaluate(
                        "() => document.querySelectorAll('input[type=\"checkbox\"]:checked').length"
                    )
                    ok = await _click_row_checkbox(page, name)
                    await asyncio.sleep(0.8)
                    after = await page.evaluate(
                        "() => document.querySelectorAll('input[type=\"checkbox\"]:checked').length"
                    )
                    if ok and after > before:
                        done = True
                        break
                    logger.info(f"[BrowserShare] {name[:20]} 勾选未生效({before}→{after})，重试 {attempt+1}/2")
                ok = done
                await asyncio.sleep(1)
                after = await page.evaluate(
                    "() => document.querySelectorAll('input[type=\"checkbox\"]:checked').length"
                )
                if ok and after > before:
                    done = True
                    break
                # 可能点成了取消勾选或没点上，重试
                logger.info(f"[BrowserShare] {name[:20]} 勾选未生效({before}→{after})，重试 {attempt+1}/2")
            if done:
                target_checked += 1
            else:
                logger.warning(f"[BrowserShare] 未勾选到: {name[:25]}（跳过）")
            await asyncio.sleep(0.5)

        if target_checked == 0:
            raise BrowserShareError("未选中任何文件")

        # 3. 分享
        if not await _click_button(page, ["分享"]):
            raise BrowserShareError("未找到分享按钮")
        await asyncio.sleep(2.5)

        # 4. 创建分享
        if not await _click_button(page, ["创建分享"], timeout_s=12):
            raise BrowserShareError("未找到创建分享按钮")
        await asyncio.sleep(2.5)

        # 5. 读链接
        link = await _read_share_link(page)
        if not link:
            await page.screenshot(path=str(Path(__file__).parent / "browser_data" / "share_dialog_fail.png"))
            raise BrowserShareError("未读取到分享链接")

        m = re.search(r"pan\.quark\.cn/s/([a-zA-Z0-9]+)", link)
        result = {"url": f"https://pan.quark.cn/s/{m.group(1)}", "passcode": ""}

        # 6. 关闭弹窗
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        await page.close()  # 用完即弃，下次全新

        logger.info(
            f"[BrowserShare] 可信UI分享成功: {result['url']} "
            f"({','.join(n[:15] for n in file_names)}) 耗时{time.time()-start:.0f}s"
        )
        return result
    finally:
        await pw.stop()


async def check_login_via_browser() -> bool:
    """检查 CDP Chrome 的登录状态"""
    ensure_cdp_chrome()
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = await context.new_page()
        await page.goto(DRIVE_URL, wait_until="domcontentloaded", timeout=30000)
        for _ in range(8):
            n = await page.evaluate("() => document.querySelectorAll('.filename-text').length")
            if n > 0:
                return True
            await asyncio.sleep(1)
        return False
    except Exception as e:
        logger.warning(f"[BrowserShare] 登录检查异常: {e}")
        return False
    finally:
        await pw.stop()


async def main_test():
    """自测：python browser_share.py <网盘文件精确名>"""
    import sys
    logging.basicConfig(level=logging.INFO)
    name = sys.argv[1] if len(sys.argv) > 1 else "蝴蝶.mp4"
    r = await create_share_via_trusted_ui([name])
    print(f"分享成功: {r['url']}")


if __name__ == "__main__":
    asyncio.run(main_test())
