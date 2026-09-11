"""
新片自动发现
读取【线上真实库】的 mac_vod（网站采集每天都在更新），
把本地任务队列里还没有的 2025/2026 院线电影补进来。

设计：
- 只读线上 mac_vod（数据最新）
- 只写本地 quark_transfer / quark_link_vod（任务队列）
- 幂等：按影片名去重，已存在的跳过
- 优先级：P300 中国院线(近60天) / P200 海外院线 / P100 其他

用法：python discover_new.py [--force]
运行器每天自动执行一次（日期去重）
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pymysql
from config import DB_CONFIG, ONLINE_DB

STATE_FILE = Path(__file__).parent / ".discover_state"
JUNK_CLASS = "纪录|记录|体育|篮球|足球|游戏|电竞|演唱会|综艺|访谈"
JUNK_NAME = "实机赏析|预告片|花絮|解说|reaction|试玩|直播回放"
# 剧集过滤：名称含"第X季"或备注含"第N集/全N集/更新至N集"的排除（剧集体积大且更新频繁）
SERIES_NAME = "第[一二三四五六七八九十0-9]+季"
SERIES_REMARKS = "(更新至|全|第)[0-9]+集"


def clean_name(s: str) -> str:
    """去掉零宽字符并压缩空白"""
    s = re.sub(r"[\u200b\u200c\u200d\ufeff\u2060]", "", s or "")
    return s.strip()[:255]


def norm(s: str) -> str:
    return clean_name(s).casefold().replace(" ", "")


def was_run_today() -> bool:
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() == datetime.now().strftime("%Y-%m-%d")
    return False


def mark_ran():
    STATE_FILE.write_text(datetime.now().strftime("%Y-%m-%d"))


def main(force: bool = False):
    if not force and was_run_today():
        print("今日已执行过新片发现，跳过")
        return 0

    # 1. 线上库查今年的院线电影
    oconn = pymysql.connect(**ONLINE_DB, charset="utf8mb4",
                            connect_timeout=10, read_timeout=60)
    ocur = oconn.cursor()
    ocur.execute(f"""
        SELECT v.vod_name, MIN(v.vod_year) yr, GROUP_CONCAT(DISTINCT v.vod_id) ids,
               MAX(v.vod_hits) hits,
               MAX(CASE WHEN v.vod_pubdate LIKE '2026%' THEN SUBSTRING(v.vod_pubdate,1,10) END) pubdate,
               MAX(CASE WHEN (v.vod_area LIKE '%%大陆%%' OR v.vod_area LIKE '%%香港%%'
                              OR v.vod_pubdate LIKE '%%中国大陆%%')
                         AND v.vod_pubdate LIKE '2026%' THEN 1 ELSE 0 END) is_cn
        FROM mac_vod v
        WHERE v.vod_year IN ('2025', '2026') AND v.type_id_1 = 1
          AND (v.vod_class IS NULL OR v.vod_class NOT REGEXP '{JUNK_CLASS}')
          AND v.vod_name NOT REGEXP '{JUNK_NAME}'
          AND v.vod_name NOT REGEXP '{SERIES_NAME}'
          AND (v.vod_remarks IS NULL OR v.vod_remarks NOT REGEXP '{SERIES_REMARKS}')
        GROUP BY v.vod_name
    """)
    movies = ocur.fetchall()
    # 线上已有链接的影片（跳过，无需重复处理）
    ocur.execute("SELECT DISTINCT vod_id FROM mac_vod_netdisk WHERE type='quark'")
    covered = {r[0] for r in ocur.fetchall()}
    oconn.close()
    print(f"线上库今年电影: {len(movies)} 部（其中有链接的 vod_id {len(covered)} 个）")

    # 2. 本地已有任务名
    lconn = pymysql.connect(**{k: DB_CONFIG[k] for k in ("host", "port", "user", "password", "database")},
                            charset="utf8mb4", autocommit=True)
    lcur = lconn.cursor()
    lcur.execute("SELECT vod_name FROM quark_transfer")
    seen = {norm(r[0]) for r in lcur.fetchall()}

    # 3. 补新片
    cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    added = 0
    for name, yr, ids, hits, pubdate, is_cn in movies:
        cname = clean_name(name)
        if not cname or norm(cname) in seen:
            continue
        vod_ids = [int(x) for x in (ids or "").split(",") if x.strip().isdigit()]
        if vod_ids and all(v in covered for v in vod_ids):
            continue  # 线上已有链接

        pub = (pubdate or "")[:10]
        pri = 300 if (is_cn and pub and pub >= cutoff) else (200 if pub else 100)
        rd = pub if re.match(r"^\d{4}-\d{2}-\d{2}$", pub) else None
        try:
            lcur.execute(
                "INSERT INTO quark_transfer (vod_name, vod_year, priority, release_date, vod_hits) "
                "VALUES (%s, %s, %s, %s, %s)",
                (cname, (yr or "")[:10], pri, rd, hits or 0),
            )
        except pymysql.err.IntegrityError:
            seen.add(norm(cname))
            continue
        seen.add(norm(cname))
        added += 1
        tid = lcur.lastrowid
        for vid in vod_ids:
            lcur.execute("INSERT IGNORE INTO quark_link_vod (transfer_id, vod_id) VALUES (%s, %s)",
                         (tid, vid))
        if added <= 10:
            print(f"  ➕ P{pri} {cname[:28]} ({pub or '无日期'})")

    lconn.close()
    mark_ran()
    print(f"\n✅ 新片发现完成: 新增 {added} 部")
    return added


if __name__ == "__main__":
    main(force="--force" in sys.argv)
