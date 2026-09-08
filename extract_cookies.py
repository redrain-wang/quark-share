"""
从 Chrome 提取夸克网盘 Cookie 并保存到本地
需要先关闭 Chrome，然后用 Playwright 启动并导入 Chrome 数据

或者：使用已登录的 Chrome 会话手动保存 Cookie
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright
from config import BROWSER_DATA_DIR


async def main():
    """
    方案：启动 Playwright Chromium，使用 Chrome 的用户数据目录
    这样可以复用 Chrome 中已登录的会话
    """
    SAVE_DIR = BROWSER_DATA_DIR
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = SAVE_DIR / "quark_1_cookies.json"

    # Chrome 用户数据目录（macOS）
    chrome_user_data = Path.home() / "Library/Application Support/Google/Chrome"

    print("正在启动浏览器（使用 Chrome 登录数据）...")
    print("如果 Chrome 正在运行，请先关闭 Chrome！")

    pw = await async_playwright().start()

    try:
        # 使用 Chrome 的用户数据目录启动 Chromium
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(chrome_user_data),
            headless=False,
            channel="chrome",  # 使用系统安装的 Chrome
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            viewport={"width": 1366, "height": 768},
            locale="zh-CN",
        )

        page = context.pages[0] if context.pages else await context.new_page()

        print("正在访问夸克网盘...")
        await page.goto("https://pan.quark.cn", wait_until="networkidle")
        await asyncio.sleep(3)

        # 截图检查状态
        await page.screenshot(path=str(SAVE_DIR / "extract_check.png"))
        print("截图已保存: extract_check.png")

        # 检查是否已登录
        page_content = await page.content()
        if "登录" in page_content and "我的网盘" not in page_content:
            print("❌ 未登录，请先在浏览器中登录夸克网盘")
        else:
            print("✅ 已登录，正在提取 Cookie...")

            # 获取所有 Cookie
            cookies = await context.cookies()

            # 只保存夸克相关的 Cookie
            quark_cookies = [
                c for c in cookies
                if "quark.cn" in c.get("domain", "") or "quark" in c.get("name", "").lower()
            ]

            if quark_cookies:
                cookie_file.write_text(json.dumps(quark_cookies, ensure_ascii=False, indent=2))
                print(f"✅ 已保存 {len(quark_cookies)} 条夸克 Cookie 到 {cookie_file}")
            else:
                # 保存所有 Cookie 以防万一
                cookie_file.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
                print(f"✅ 已保存全部 {len(cookies)} 条 Cookie 到 {cookie_file}")

        await context.close()

    except Exception as e:
        print(f"错误: {e}")
        print("\n提示：请先关闭 Chrome 浏览器，然后重新运行此脚本")
    finally:
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
