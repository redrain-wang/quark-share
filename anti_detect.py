"""
反检测与风控模块
负责随机延迟、频率控制、每日上限管理
"""
import asyncio
import logging
import random
import time
from collections import defaultdict

from config import ANTI_DETECT

logger = logging.getLogger(__name__)


# 每个账号当天的已转存次数统计
_daily_counts: dict[int, int] = defaultdict(int)
_daily_date: dict[int, str] = {}
_consecutive_counts: dict[int, int] = defaultdict(int)


def _today_str() -> str:
    return time.strftime("%Y-%m-%d")


async def random_delay(delay_type: str):
    """
    根据延迟类型执行随机等待。
    delay_type 对应 config.ANTI_DETECT["delays"] 中的 key。
    """
    ranges = ANTI_DETECT["delays"].get(delay_type, (1, 3))
    delay = random.uniform(*ranges)
    logger.debug(f"随机延迟 [{delay_type}]: {delay:.1f}s")
    await asyncio.sleep(delay)


async def check_daily_limit(account_id: int, disk_type: str) -> bool:
    """
    检查账号今天是否已达转存上限。
    返回 True 表示还可以继续，False 表示已达上限。
    """
    today = _today_str()

    # 日期切换时重置计数
    if _daily_date.get(account_id) != today:
        _daily_counts[account_id] = 0
        _daily_date[account_id] = today

    limit = ANTI_DETECT["daily_limits"].get(disk_type, 30)
    current = _daily_counts[account_id]

    if current >= limit:
        logger.warning(
            f"账号 {account_id} 今日已转存 {current} 次, 达到上限 {limit}"
        )
        return False

    return True


def record_save(account_id: int):
    """记录一次转存操作"""
    today = _today_str()
    if _daily_date.get(account_id) != today:
        _daily_counts[account_id] = 0
        _daily_date[account_id] = today

    _daily_counts[account_id] += 1
    _consecutive_counts[account_id] += 1

    logger.info(
        f"账号 {account_id} 今日已转存 {_daily_counts[account_id]} 次, "
        f"连续 {_consecutive_counts[account_id]} 次"
    )


async def check_consecutive_break(account_id: int) -> bool:
    """
    检查是否需要强制休息（连续转存过多）。
    返回 True 表示需要休息，False 表示可以继续。
    """
    max_consecutive = ANTI_DETECT["max_consecutive"]
    consecutive = _consecutive_counts[account_id]

    if consecutive >= max_consecutive:
        break_range = ANTI_DETECT["forced_break"]
        break_time = random.uniform(*break_range)
        logger.warning(
            f"账号 {account_id} 连续转存 {consecutive} 次, "
            f"强制休息 {break_time:.0f}s"
        )
        await asyncio.sleep(break_time)
        _consecutive_counts[account_id] = 0
        return True

    return False


def reset_consecutive(account_id: int):
    """重置连续转存计数（比如换关键词后调用）"""
    _consecutive_counts[account_id] = 0


def get_stats() -> dict:
    """获取当前统计信息，用于日志输出"""
    today = _today_str()
    stats = {}
    for aid in _daily_counts:
        if _daily_date.get(aid) == today:
            stats[aid] = {
                "today_count": _daily_counts[aid],
                "consecutive": _consecutive_counts[aid],
            }
    return stats
