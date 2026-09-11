"""
配置文件 - 所有可配置项集中管理
敏感信息（数据库密码等）支持环境变量覆盖，默认值仅用于本地开发
"""
import os
import re
from pathlib import Path

# ============================================================
#  基础路径
# ============================================================
BASE_DIR = Path(__file__).parent
BROWSER_DATA_DIR = BASE_DIR / "browser_data"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
REPORTS_DIR = BASE_DIR / "reports"

# ============================================================
#  本地 MySQL（Docker，处理用工作库）
# ============================================================
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "easysvip123"),
    "database": os.getenv("DB_NAME", "easysvip"),
    "charset": "utf8mb4",
    "autocommit": True,
}

# ============================================================
#  线上生产库（网站正在使用的库，同步目标）
#  连接信息从 db_online.ini 读取（该文件不入库，见 .gitignore）
# ============================================================
ONLINE_DB_INI = BASE_DIR / "db_online.ini"


def _load_online_db() -> dict:
    """解析 db_online.ini（地址/账号/密码 三行格式）"""
    cfg = {
        "host": os.getenv("ONLINE_DB_HOST", ""),
        "port": int(os.getenv("ONLINE_DB_PORT", 3306)),
        "user": os.getenv("ONLINE_DB_USER", ""),
        "password": os.getenv("ONLINE_DB_PASSWORD", ""),
        "database": os.getenv("ONLINE_DB_NAME", "easysvip"),
    }
    if ONLINE_DB_INI.exists():
        text = ONLINE_DB_INI.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            for zh_key, en_key in (
                ("地址", "host"), ("账号", "user"), ("密码", "password")
            ):
                m = re.search(rf"{zh_key}[：:]\s*([^\s]+)", line)
                if m:
                    cfg[en_key] = m.group(1).strip()
    return cfg


ONLINE_DB = _load_online_db()

# ============================================================
#  PanSou 多源搜索（按优先级降级：远程TG → 远程插件 → 本地）
# ============================================================
SEARCH_ENDPOINTS = [
    ("https://so.252035.xyz/api/search", "tg"),
    ("https://so.easysvip.com/api/search", "tg"),
    ("https://so.252035.xyz/api/search", "plugin"),
    ("https://so.easysvip.com/api/search", "plugin"),
    ("http://127.0.0.1:8888/api/search", None),
]
PANSOU_CONCURRENCY = 5
PANSOU_RESULT_TYPE = "merge"

# ============================================================
#  多账号配置
#  每个账号独立 Chrome 配置 + 独立调试端口（登录态隔离）
#  账号1沿用既有路径，避免丢失已登录会话
# ============================================================
def get_account_profile(account_id: int) -> dict:
    """按账号 ID 返回 Chrome 配置/端口/Cookie 文件路径"""
    if account_id == 1:
        return {
            "profile_dir": "/tmp/chrome_cdp_profile",
            "cdp_port": 9223,
            "cookie_file": BROWSER_DATA_DIR / "quark_1_cookies.json",
        }
    return {
        "profile_dir": f"/tmp/chrome_cdp_profile_{account_id}",
        "cdp_port": 9222 + account_id,  # 账号2 → 9224
        "cookie_file": BROWSER_DATA_DIR / f"quark_{account_id}_cookies.json",
    }


def account_cdp_url(account_id: int) -> str:
    return f"http://127.0.0.1:{get_account_profile(account_id)['cdp_port']}"


# ============================================================
#  转存目标
# ============================================================
# 夸克网盘中接收转存内容的文件夹名
TARGET_FOLDER = "easysvip.com"
# 网站详情页地址模板（报告用）
SITE_DETAIL_URL = "https://easysvip.com/index.php/vod/detail/id/{vid}.html"

# ============================================================
#  反风控 / 调度参数
# ============================================================
ANTI_DETECT = {
    # 每个账号每日转存上限
    "daily_limits": {
        "quark": 50,
        "baidu": 30,
        "aliyun": 40,
    },
    # 操作间隔（秒）—— 随机取 [min, max]
    "delays": {
        "between_saves": (30, 90),
        "between_keywords": (120, 300),
        "page_load": (2, 5),
        "typing_char": (0.05, 0.15),
        "after_click": (1, 3),
    },
    # 单次最大连续转存数，超过后强制休息
    "max_consecutive": 10,
    # 强制休息时长（秒）
    "forced_break": (300, 600),
}

# ============================================================
#  日志
# ============================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = BASE_DIR / "service.log"
