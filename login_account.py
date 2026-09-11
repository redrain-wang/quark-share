"""
新账号登录工具（手机验证码方式，可信鼠标事件驱动）

用法：
  python login_account.py send-code <账号ID>    # 打开浏览器、填手机号、发送验证码
  python login_account.py verify <账号ID> <验证码>  # 输入验证码完成登录并保存 Cookie

说明：账号 ID 对应 cloud_accounts 表；每个账号用独立 Chrome 配置和调试端口。
"""
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pymysql
from config import DB_CONFIG, BROWSER_DATA_DIR, get_account_profile


def get_account(account_id: int) -> dict:
    conn = pymysql.connect(**{k: DB_CONFIG[k] for k in ("host", "port", "user", "password", "database")},
                           charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    cur.execute("SELECT id, disk_type, phone FROM cloud_accounts WHERE id=%s", (account_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"账号 {account_id} 不存在")
    return {"id": row[0], "disk_type": row[1], "phone": row[2]}


def launch_chrome(profile_dir: str, port: int):
    """用独立空配置启动 Chrome（不复制主配置，避免带入其他账号登录态）"""
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        ["open", "-na", "Google Chrome", "--args",
         f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}", "--no-first-run"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def page_click_text(page, texts, timeout=8):
    """可信鼠标点击文本匹配元素"""
    import random
    start = time.time()
    while time.time() - start < timeout:
        box = await page.evaluate("""
            (names) => {
                const els = document.querySelectorAll('button, span, a, div[role="button"]');
                for (const el of els) {
                    const t = (el.textContent || '').trim();
                    if (names.some(n => t === n || t.includes(n)) && el.offsetWidth > 0) {
                        const r = el.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2};
                    }
                }
                return null;
            }
        """, texts)
        if box:
            cx, cy = box["x"] - random.uniform(80, 180), box["y"] + random.uniform(-50, 50)
            await page.mouse.move(cx, cy)
            for i in range(1, 6):
                await page.mouse.move(cx + (box["x"]-cx)*i/5, cy + (box["y"]-cy)*i/5)
                await asyncio.sleep(0.03)
            await page.mouse.click(box["x"], box["y"])
            return True
        await asyncio.sleep(0.7)
    return False


async def send_code(account_id: int):
    acc = get_account(account_id)
    prof = get_account_profile(account_id)
    launch_chrome(prof["profile_dir"], prof["cdp_port"])
    await asyncio.sleep(8)

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{prof['cdp_port']}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://pan.quark.cn", wait_until="domcontentloaded")
        await asyncio.sleep(4)

        # 进入登录 → 手机号登录
        await page_click_text(page, ["登录", "立即登录"], timeout=6)
        await asyncio.sleep(2)
        await page_click_text(page, ["手机号登录", "验证码登录", "手机号"], timeout=6)
        await asyncio.sleep(2)

        # 填手机号
        filled = await page.evaluate("""
            (phone) => {
                const inputs = document.querySelectorAll('input');
                for (const i of inputs) {
                    const ph = (i.placeholder || '') + (i.type || '');
                    if (/手机号|phone|tel/i.test(ph) && i.offsetWidth > 0) {
                        i.focus();
                        i.value = phone;
                        i.dispatchEvent(new Event('input', {bubbles: true}));
                        return 'ok';
                    }
                }
                return 'no-input';
            }
        """, acc["phone"])
        print(f"填手机号: {filled}")
        await asyncio.sleep(1)

        # 点发送验证码
        sent = await page_click_text(page, ["获取验证码", "发送验证码"], timeout=6)
        print(f"发送验证码: {sent}")
        print("\n👉 验证码已发送，请把收到的 6 位验证码告诉我，我继续完成登录")
        print("   （浏览器窗口已打开，可用 python login_account.py verify %d <验证码> 完成）" % account_id)
    finally:
        await pw.stop()


async def verify(account_id: int, code: str):
    acc = get_account(account_id)
    prof = get_account_profile(account_id)
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{prof['cdp_port']}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # 填验证码
        filled = await page.evaluate("""
            (code) => {
                const inputs = document.querySelectorAll('input');
                let n = 0;
                for (const i of inputs) {
                    const ph = (i.placeholder || '') + (i.type || '');
                    if ((/验证码|code/i.test(ph) || i.maxLength === 6) && i.offsetWidth > 0) {
                        i.focus();
                        i.value = code;
                        i.dispatchEvent(new Event('input', {bubbles: true}));
                        n++;
                    }
                }
                return n;
            }
        """, code)
        print(f"填入验证码输入框数: {filled}")
        await asyncio.sleep(1)

        await page_click_text(page, ["登录", "确定", "提交"], timeout=6)
        await asyncio.sleep(5)

        # 验证登录并保存 Cookie
        ok = await page.evaluate("""() => document.querySelectorAll('.filename-text').length > 0
                                     || location.href.includes('/list')""")
        print(f"登录状态: {'✅ 成功' if ok else '❓ 未确认'}")

        cookies = await ctx.cookies()
        quark = [c for c in cookies if "quark" in c.get("domain", "")]
        if not quark:
            quark = cookies
        cookie_file = BROWSER_DATA_DIR / f"quark_{account_id}_cookies.json"
        cookie_file.write_text(json.dumps(quark, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ Cookie 已保存: {cookie_file} ({len(quark)} 条)")
    finally:
        await pw.stop()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "send-code":
        asyncio.run(send_code(int(sys.argv[2])))
    elif cmd == "verify":
        asyncio.run(verify(int(sys.argv[2]), sys.argv[3]))
    else:
        print(__doc__)
