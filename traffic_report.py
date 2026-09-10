"""
网盘链接点击流量报告
从线上库 mac_netdisk_click 聚合点击数据，生成日报

用法：python traffic_report.py
运行器每天自动执行一次
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pymysql
from config import ONLINE_DB

REPORT_DIR = Path(__file__).parent / "reports"


def main():
    conn = pymysql.connect(**ONLINE_DB, charset="utf8mb4",
                           connect_timeout=10, read_timeout=30)
    cur = conn.cursor()

    # 表可能还没建（端点未被调用过）
    cur.execute("SHOW TABLES LIKE 'mac_netdisk_click'")
    if not cur.fetchone():
        print("点击表尚未创建（网站端点还没部署或没人点击）")
        conn.close()
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("SELECT COUNT(*) FROM mac_netdisk_click WHERE DATE(click_time)=CURDATE()")
    today_clicks = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM mac_netdisk_click")
    total_clicks = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT ip_hash) FROM mac_netdisk_click WHERE DATE(click_time)=CURDATE()")
    today_uv = cur.fetchone()[0]

    # 今日点击排行
    cur.execute("""
        SELECT c.vod_id, v.vod_name, COUNT(*) cnt
        FROM mac_netdisk_click c LEFT JOIN mac_vod v ON v.vod_id = c.vod_id
        WHERE DATE(c.click_time) = CURDATE()
        GROUP BY c.vod_id, v.vod_name ORDER BY cnt DESC LIMIT 20
    """)
    top_today = cur.fetchall()

    # 累计排行
    cur.execute("""
        SELECT c.vod_id, v.vod_name, COUNT(*) cnt
        FROM mac_netdisk_click c LEFT JOIN mac_vod v ON v.vod_id = c.vod_id
        GROUP BY c.vod_id, v.vod_name ORDER BY cnt DESC LIMIT 20
    """)
    top_all = cur.fetchall()
    conn.close()

    lines = [f"# 网盘点击流量报告 · {today}", ""]
    lines.append(f"今日点击 **{today_clicks}** 次 ｜ 今日访客 **{today_uv}** 人 ｜ 累计点击 **{total_clicks}** 次")
    lines.append("")
    lines.append("## 今日点击排行")
    lines.append("")
    lines.append("| # | 影片 | 点击 |")
    lines.append("|---|------|------|")
    for i, (vid, name, cnt) in enumerate(top_today, 1):
        lines.append(f"| {i} | {name or ('vod_id='+str(vid))} | {cnt} |")
    lines.append("")
    lines.append("## 累计点击排行")
    lines.append("")
    lines.append("| # | 影片 | 点击 |")
    lines.append("|---|------|------|")
    for i, (vid, name, cnt) in enumerate(top_all, 1):
        lines.append(f"| {i} | {name or ('vod_id='+str(vid))} | {cnt} |")
    lines.append("")
    lines.append("---")
    lines.append("*数据来源：网站 netdisk_click.php 埋点 · 用户点击转存按钮即计数*")

    REPORT_DIR.mkdir(exist_ok=True)
    out = REPORT_DIR / f"流量报告_{today}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 流量报告: {out} (今日 {today_clicks} 次点击 / {today_uv} 访客)")
    return today_clicks


if __name__ == "__main__":
    main()
