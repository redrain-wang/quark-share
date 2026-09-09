"""
把生成的网盘资源汇总页部署到网站仓库（easysvip）

流程：
1. 复制 reports/网盘资源汇总.html → 网站根目录 netdisk.html
2. 在网站仓库提交并推送到当前分支（release）
   服务器定时 git pull 即可上线：https://easysvip.com/netdisk.html

用法：python push_site_page.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
SOURCE_HTML = BASE_DIR / "reports" / "网盘资源汇总.html"
SITE_DIR = Path.home() / "Documents" / "easysvip"
TARGET = SITE_DIR / "netdisk_share.html"


def run(cmd: list[str], cwd: Path | None = None) -> bool:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0
    if not ok:
        print(f"  ⚠️ {' '.join(cmd[:3])} 失败: {r.stderr.strip()[:150]}")
    return ok


def main() -> bool:
    if not SOURCE_HTML.exists():
        print("  汇总页尚未生成，跳过部署")
        return False

    if not (SITE_DIR / ".git").exists():
        print(f"  网站仓库不存在: {SITE_DIR}")
        return False

    # 1. 复制到网站根目录
    shutil.copy(SOURCE_HTML, TARGET)

    # 2. 提交推送（只提交 netdisk.html，不动仓库里其他文件）
    ok = True
    if not run(["git", "add", "netdisk_share.html"], cwd=SITE_DIR):
        return False
    # 无变化时 commit 会失败，属正常
    committed = run(["git", "commit", "-m",
                     "update: 网盘资源汇总页自动更新"], cwd=SITE_DIR)
    if committed:
        ok = run(["git", "push", "origin", "release"], cwd=SITE_DIR)
        if ok:
            print("  ✅ 已推送 release 分支（服务器拉取后上线）")
    else:
        print("  页面无变化，跳过推送")
    return ok


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
