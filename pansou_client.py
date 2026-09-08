"""
PanSou API 客户端
调用 PanSou 搜索接口获取网盘链接

支持多个 PanSou 端点 + 多来源（tg/plugin）降级策略：
1. 远程 TG 源（快，有缓存）
2. 远程插件源
3. 本地实例
"""
import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

# 搜索端点列表（按优先级，TG 源最快优先）
SEARCH_ENDPOINTS = [
    # (url, src) — src 为 None 表示不指定
    ("https://so.252035.xyz/api/search", "tg"),
    ("https://so.easysvip.com/api/search", "tg"),
    ("https://so.252035.xyz/api/search", "plugin"),
    ("https://so.easysvip.com/api/search", "plugin"),
    ("http://127.0.0.1:8888/api/search", None),
]

PANSOU_CONCURRENCY = 5
PANSOU_RESULT_TYPE = "merge"


@dataclass
class DiskLink:
    """单个网盘链接"""
    url: str
    password: str
    disk_type: str    # quark / baidu / aliyun / ...
    source: str       # 来源插件或TG频道
    note: str = ""    # 标题/描述
    datetime: str = ""


@dataclass
class SearchResult:
    """搜索结果"""
    keyword: str
    total: int
    links: list[DiskLink]


async def search_links(
    keyword: str,
    cloud_types: list[str] | None = None,
    max_polls: int = 3,
    poll_interval: int = 5,
) -> SearchResult:
    """
    按端点优先级搜索关键词，返回网盘链接。

    端点降级策略：远程TG → 远程插件 → 本地。
    单个端点内部做少量轮询（应对异步缓存）。

    Args:
        keyword: 搜索关键词
        cloud_types: 只搜索指定网盘类型, 如 ["quark"]
        max_polls: 单端点最大轮询次数
        poll_interval: 轮询间隔（秒）
    """
    best: SearchResult = SearchResult(keyword=keyword, total=0, links=[])

    for endpoint, src in SEARCH_ENDPOINTS:
        try:
            result = await _search_one_endpoint(
                endpoint, src, keyword, cloud_types, max_polls, poll_interval
            )
            if len(result.links) > len(best.links):
                best = result
            # 拿到足够多的链接就不再降级
            if len(best.links) >= 5:
                break
        except Exception as e:
            logger.warning(f"端点 {endpoint}(src={src}) 异常 [{type(e).__name__}]: {e}")

    logger.info(
        f"PanSou 搜索 '{keyword}': 共提取 {len(best.links)} 条链接 "
        f"(端点{len(SEARCH_ENDPOINTS)}个)"
    )
    return best


async def _search_one_endpoint(
    endpoint: str,
    src: str | None,
    keyword: str,
    cloud_types: list[str] | None,
    max_polls: int,
    poll_interval: int,
) -> SearchResult:
    """在单个端点上搜索（带轮询）"""
    params = {
        "kw": keyword,
        "conc": PANSOU_CONCURRENCY,
        "res": PANSOU_RESULT_TYPE,
    }
    if src:
        params["src"] = src
    if cloud_types:
        params["cloud_types"] = ",".join(cloud_types)

    links: list[DiskLink] = []
    total = 0
    last_count = -1
    stable_rounds = 0

    async with aiohttp.ClientSession() as session:
        for poll in range(max_polls):
            async with session.get(
                endpoint,
                params=params,
                timeout=aiohttp.ClientTimeout(total=35),
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"[{endpoint}] HTTP {resp.status}")
                    break

                data = await resp.json()

                if data.get("code") != 0:
                    logger.debug(f"[{endpoint}] API 错误: {data.get('message')}")
                    break

                api_data = data.get("data", {})
                total = api_data.get("total", 0)

                links = []
                merged = api_data.get("merged_by_type", {})
                for disk_type, link_list in merged.items():
                    if disk_type in ("magnet", "ed2k", "others"):
                        continue
                    for item in link_list:
                        links.append(DiskLink(
                            url=item.get("url", ""),
                            password=item.get("password", ""),
                            disk_type=disk_type,
                            source=item.get("source", ""),
                            note=item.get("note", "")[:200],
                            datetime=item.get("datetime", ""),
                        ))

            # 稳定判断：连续2轮不变则结束
            if len(links) == last_count:
                stable_rounds += 1
                if stable_rounds >= 2:
                    break
            else:
                stable_rounds = 0
            last_count = len(links)

            if len(links) >= 5 and poll >= 1:
                break

            if poll < max_polls - 1:
                await asyncio.sleep(poll_interval)

    return SearchResult(keyword=keyword, total=total, links=links)
