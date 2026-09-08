"""
每日转存报告生成器
每轮结束 / 每天自动生成转存记录文档

输出：
  reports/转存记录_YYYY-MM-DD.md     按天的明细
  reports/转存总表.md                累计全量（覆盖更新）

用法：python daily_report.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pymysql
from config import DB_CONFIG
from datetime import datetime

REPORT_DIR = Path(__file__).parent / "reports"
SITE_URL = "https://easysvip.com/index.php/vod/detail/id/{vid}.html"

STATUS_MAP = {0: "待处理", 2: "已转存", 3: "已完成", 4: "无资源", 5: "待分享"}


def get_conn():
    return pymysql.connect(
        **{k: DB_CONFIG[k] for k in ("host", "port", "user", "password", "database")},
        charset="utf8mb4", autocommit=True, read_timeout=15,
    )


def fetch_today(cur) -> list[dict]:
    cur.execute("""
        SELECT t.id, t.vod_name, t.my_url, t.my_pwd, t.release_date, t.priority,
               t.updated_at, GROUP_CONCAT(m.vod_id) vod_ids
        FROM quark_transfer t
        LEFT JOIN quark_link_vod m ON m.transfer_id = t.id
        WHERE t.status = 3 AND t.my_url != '' AND DATE(t.updated_at) = CURDATE()
        GROUP BY t.id
        ORDER BY t.updated_at DESC
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def fetch_all(cur) -> list[dict]:
    cur.execute("""
        SELECT t.id, t.vod_name, t.my_url, t.my_pwd, t.release_date, t.priority,
               t.updated_at, GROUP_CONCAT(m.vod_id) vod_ids
        FROM quark_transfer t
        LEFT JOIN quark_link_vod m ON m.transfer_id = t.id
        WHERE t.status = 3 AND t.my_url != ''
        GROUP BY t.id
        ORDER BY t.updated_at DESC
    """)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def render_md(rows: list[dict], title: str, subtitle: str = "") -> str:
    lines = [f"# {title}", ""]
    if subtitle:
        lines += [f"> {subtitle}", ""]
    lines.append(f"共 **{len(rows)}** 部影片 ｜ 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("| # | 视频 ID | 视频名称 | 夸克分享链接 | 网站详情页 | 上映日期 | 关联ID数 |")
    lines.append("|---|---------|----------|--------------|------------|----------|----------|")
    for i, r in enumerate(rows, 1):
        vid_ids = (r.get("vod_ids") or "").strip()
        vid_count = len([x for x in vid_ids.split(",") if x.strip()])
        detail_url = SITE_URL.format(vid=vid_ids.split(",")[0]) if vid_ids else "—"
        rel = str(r.get("release_date") or "—")
        lines.append(
            f"| {i} | {vid_ids or '—'} | **{r['vod_name']}** "
            f"| [{r['my_url'].replace('https://pan.quark.cn/s/', '')}]({r['my_url']}) "
            f"| [详情页]({detail_url}) | {rel} | {vid_count} |"
        )
    lines.append("")
    lines.append("---")
    lines.append(f"*由 daily_report.py 自动生成 · 数据来源：quark_transfer + quark_link_vod*")
    return "\n".join(lines)


def main():
    REPORT_DIR.mkdir(exist_ok=True)
    conn = get_conn()
    cur = conn.cursor()

    today_rows = fetch_today(cur)
    all_rows = fetch_all(cur)
    conn.close()

    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 按天明细
    today_md = render_md(
        today_rows,
        f"转存记录 · {today}",
        "今日完成的影片（含夸克分享链接与网站详情页地址）",
    )
    today_file = REPORT_DIR / f"转存记录_{today}.md"
    today_file.write_text(today_md, encoding="utf-8")
    print(f"✅ 日报已生成: {today_file} ({len(today_rows)} 部)")

    # 2. 累计总表
    all_md = render_md(
        all_rows,
        "转存总表 · 全部完成影片",
        f"累计完成 {len(all_rows)} 部，持续更新中",
    )
    all_file = REPORT_DIR / "转存总表.md"
    all_file.write_text(all_md, encoding="utf-8")
    print(f"✅ 总表已更新: {all_file} ({len(all_rows)} 部)")


if __name__ == "__main__":
    main()
