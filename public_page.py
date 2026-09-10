"""
公开版网盘资源汇总页面生成器
从线上库渲染一个可挂到网站上的静态 HTML 页面（自包含，无外部依赖）

产出：reports/网盘资源汇总.html
部署：将该文件上传到网站（如 /netdisk.html），加导航入口即可
运行器每轮结束自动重新生成，数据始终最新

用法：python public_page.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import ONLINE_DB, SITE_DETAIL_URL

OUTPUT = Path(__file__).parent / "reports" / "网盘资源汇总.html"


async def fetch_movies() -> list[dict]:
    """从线上库拉取全部有夸克链接的影片（去重，取最新链接）"""
    import aiomysql
    conn = await aiomysql.connect(
        host=ONLINE_DB["host"], port=ONLINE_DB["port"],
        user=ONLINE_DB["user"], password=ONLINE_DB["password"],
        db=ONLINE_DB["database"], charset="utf8mb4",
        connect_timeout=10, autocommit=True,
    )
    try:
        cur = await conn.cursor()
        await cur.execute("""
            SELECT n.vod_id, v.vod_name, v.vod_year, n.share_url
            FROM mac_vod_netdisk n
            JOIN mac_vod v ON v.vod_id = n.vod_id
            WHERE n.type = 'quark'
            ORDER BY v.vod_year DESC, v.vod_name, n.id DESC
        """)
        rows = await cur.fetchall()
    finally:
        conn.close()

    # 同一影片多 vod_id 合并，链接取该影片最新的（vod_id 最大者通常最新）
    movies = {}
    for vid, name, year, url in rows:
        key = name.strip()
        if key not in movies:
            movies[key] = {"year": year or "", "vod_ids": [], "url": url}
        movies[key]["vod_ids"].append(vid)
        movies[key]["url"] = url  # 排序按 id DESC，最后一个最新

    return [
        {"name": k, "year": v["year"], "vod_ids": sorted(set(v["vod_ids"])), "url": v["url"]}
        for k, v in movies.items()
    ]


def render_html(movies: list[dict]) -> str:
    """服务端直出（SSR）渲染：所有影片静态写入 HTML，爬虫无需执行 JS 即可抓取"""
    import json
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(movies)

    # 年份分组
    years = {}
    for m in movies:
        y = (m["year"] or "更早")[:4]
        years[y] = years.get(y, 0) + 1
    year_chips = "".join(
        f'<button class="chip{" active" if i == 0 else ""}" data-y="{y}">{y}（{c}）</button>'
        for i, (y, c) in enumerate(
            sorted(years.items(), key=lambda x: (-int(x[0]) if x[0].isdigit() else 0, x[0]))
        )
    )

    # ===== SSR：所有条目静态输出 =====
    rows_html = []
    for i, m in enumerate(movies, 1):
        vid = m["vod_ids"][0]
        detail = "https://easysvip.com/index.php/vod/detail/id/%d.html" % vid
        year = (m["year"] or "更早")[:4]
        url = m["url"].replace("http://", "https://")
        rows_html.append(
            f'<div class="item" data-name="{m["name"]}" data-year="{year}">'
            f'<div class="info"><div class="name">{m["name"]}</div>'
            f'<div class="sub">{year} 年 · 夸克网盘</div></div>'
            f'<div class="actions">'
            f'<a class="btn quark" href="{url}" target="_blank" rel="nofollow" '
            f'onclick="trackClick({vid}, this.href)">💾 夸克网盘转存</a>'
            f'<a class="btn detail" href="{detail}" target="_blank">详情</a>'
            f'</div></div>'
        )
    rows = "\n".join(rows_html)

    # 结构化数据（ItemList，帮助搜索引擎理解列表）
    ld_items = [
        {"@type": "ListItem", "position": i + 1, "name": m["name"],
         "url": "https://easysvip.com/index.php/vod/detail/id/%d.html" % m["vod_ids"][0]}
        for i, m in enumerate(movies[:200])
    ]
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "网盘资源汇总",
        "numberOfItems": total,
        "itemListElement": ld_items,
    }, ensure_ascii=False)

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>网盘资源汇总 - 最新影视夸克网盘链接（持续更新）| easysvip.com</title>
<meta name="description" content="easysvip.com 网盘资源汇总：收录 {total} 部最新影视夸克网盘分享链接，包含 {now.split()[0]} 更新的院线新片、热门剧集，点击即可转存观看。">
<meta name="keywords" content="夸克网盘,网盘资源,影视资源,高清电影,网盘链接,夸克分享,最新电影">
<meta name="robots" content="index, follow, max-image-preview:large">
<link rel="canonical" href="https://easysvip.com/netdisk_share.html">
<meta property="og:type" content="website">
<meta property="og:title" content="网盘资源汇总 - 最新影视夸克网盘链接">
<meta property="og:description" content="收录 {total} 部影视的夸克网盘分享链接，持续更新">
<meta property="og:url" content="https://easysvip.com/netdisk_share.html">
<meta property="og:site_name" content="easysvip.com">
<meta name="twitter:card" content="summary">
<script type="application/ld+json">{jsonld}</script>
<style>
  :root {{
    --primary: #4f6ef7; --primary-dark: #3d56d9;
    --bg: #f5f7fb; --card: #ffffff; --text: #1f2430;
    --muted: #6b7280; --border: #e5e9f2; --ok: #16a34a;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text);
        font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
  .container {{ max-width: 960px; margin: 0 auto; padding: 0 16px 60px; }}
  header {{
    background: linear-gradient(135deg, #4f6ef7 0%, #7c5cf0 100%);
    color: #fff; padding: 40px 16px 32px; text-align: center; border-radius: 0 0 20px 20px;
  }}
  header .site {{ font-size: 13px; opacity: .85; letter-spacing: 2px; }}
  header h1 {{ font-size: 30px; margin: 6px 0 10px; }}
  header .meta {{ font-size: 14px; opacity: .9; }}
  header .meta b {{ font-size: 18px; }}
  .toolbar {{ margin: -22px auto 0; max-width: 860px; padding: 0 16px; position: relative; z-index: 2; }}
  .search-box {{
    background: var(--card); border-radius: 14px; padding: 14px;
    box-shadow: 0 8px 24px rgba(30,40,90,.10); display: flex; gap: 10px;
  }}
  .search-box input {{
    flex: 1; border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; font-size: 15px; outline: none;
  }}
  .search-box input:focus {{ border-color: var(--primary); }}
  .chips {{ margin: 14px 0 4px; display: flex; flex-wrap: wrap; gap: 8px; }}
  .chip {{
    border: 1px solid var(--border); background: var(--card); color: var(--muted);
    padding: 5px 14px; border-radius: 18px; font-size: 13px; cursor: pointer;
  }}
  .chip.active {{ background: var(--primary); border-color: var(--primary); color: #fff; }}
  .count {{ color: var(--muted); font-size: 13px; margin: 10px 4px; }}
  .intro {{ color: var(--muted); font-size: 13px; line-height: 1.8; margin: 8px 4px 14px; }}
  .list {{ display: flex; flex-direction: column; gap: 10px; }}
  .item {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; display: flex; align-items: center; gap: 12px;
  }}
  .item:hover {{ border-color: var(--primary); box-shadow: 0 4px 14px rgba(79,110,247,.10); }}
  .item .info {{ flex: 1; min-width: 0; }}
  .item .name {{ font-weight: 600; font-size: 15px; white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; }}
  .item .sub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  .btn {{
    display: inline-block; padding: 8px 16px; border-radius: 10px;
    font-size: 13px; text-decoration: none; white-space: nowrap;
  }}
  .btn.quark {{ background: linear-gradient(135deg,#4f6ef7,#7c5cf0); color: #fff; font-weight: 600; }}
  .btn.quark:hover {{ filter: brightness(1.08); }}
  .btn.detail {{ background: var(--bg); border: 1px solid var(--border); color: var(--muted); }}
  .btn.detail:hover {{ color: var(--primary); border-color: var(--primary); }}
  .empty {{ text-align: center; color: var(--muted); padding: 40px; display: none; }}
  footer {{ text-align: center; color: var(--muted); font-size: 13px; margin-top: 40px; }}
  footer a {{ color: var(--primary); text-decoration: none; }}
  @media (max-width: 560px) {{
    .item {{ flex-direction: column; align-items: stretch; }}
    .item .actions {{ display: flex; gap: 8px; }}
    .item .actions .btn {{ text-align: center; }}
  }}
</style>
</head>
<body>
<header>
  <div class="container" style="padding-bottom:0">
    <div class="site">EASYSVIP.COM</div>
    <h1>☁️ 网盘资源汇总</h1>
    <div class="meta">收录 <b>{total}</b> 部影视资源 · 更新于 {now}</div>
  </div>
</header>

<div class="toolbar">
  <div class="search-box">
    <input id="q" type="search" placeholder="🔍 输入影片名称搜索..." autocomplete="off">
  </div>
  <div class="chips" id="chips">{year_chips}</div>
</div>

<div class="container">
  <p class="intro">本页汇总 easysvip.com 收录的影视夸克网盘分享链接，涵盖院线新片、热门剧集、经典影片，每日持续更新。点击「夸克网盘转存」即可保存到自己的网盘观看。</p>
  <div class="count" id="count">共 {total} 部资源</div>
  <div class="list" id="list">
{rows}
  </div>
  <div class="empty" id="empty">没有匹配的资源，换个关键词试试</div>
  <div style="text-align:center;margin-top:18px">
    <button class="chip" id="more">加载更多</button>
  </div>
</div>

<footer>
  数据持续更新 · 更多影视请访问 <a href="https://easysvip.com">easysvip.com</a><br>
  <span style="font-size:12px">所有资源均来自网络收集，仅供学习交流，请于 24 小时内删除</span>
</footer>

<script>
// 渐进增强：爬虫拿到完整静态列表；用户端提供搜索/筛选/分页
var PAGE = 50, shown = PAGE;
var items = Array.prototype.slice.call(document.querySelectorAll('#list .item'));
var curYear = 'all', curKw = '';

function trackClick(vodId, url) {{
  try {{
    var data = JSON.stringify({{vod_id: vodId, share_url: url}});
    if (navigator.sendBeacon) {{
      navigator.sendBeacon('https://easysvip.com/netdisk_click.php', new Blob([data], {{type: 'application/json'}}));
    }} else {{
      fetch('https://easysvip.com/netdisk_click.php', {{method: 'POST', body: data, keepalive: true}});
    }}
  }} catch (e) {{}}
}}

function apply() {{
  var kw = curKw.trim().toLowerCase(), shownCount = 0, matched = 0;
  items.forEach(function(el) {{
    var ok = (curYear === 'all' || el.dataset.year === curYear) &&
             (!kw || el.dataset.name.toLowerCase().indexOf(kw) >= 0);
    if (!ok) {{ el.style.display = 'none'; return; }}
    matched++;
    if (shownCount < shown) {{ el.style.display = ''; shownCount++; }}
    else {{ el.style.display = 'none'; }}
  }});
  document.getElementById('count').textContent = '共 ' + matched + ' 部资源';
  document.getElementById('empty').style.display = matched ? 'none' : 'block';
  document.getElementById('more').style.display = matched > shown ? '' : 'none';
}}

document.getElementById('q').addEventListener('input', function(e) {{
  curKw = e.target.value; shown = PAGE; apply();
}});
document.querySelectorAll('.chip[data-y]').forEach(function(b) {{
  b.addEventListener('click', function() {{
    document.querySelectorAll('.chip[data-y]').forEach(function(x) {{ x.classList.remove('active'); }});
    b.classList.add('active');
    curYear = b.dataset.y; shown = PAGE; apply();
  }});
}});
document.getElementById('more').addEventListener('click', function() {{ shown += PAGE; apply(); }});
apply();
</script>
</body>
</html>'''


def render_sitemap(movies: list[dict]) -> str:
    """生成 sitemap（汇总页 + 有链接影片的详情页），提升收录效率"""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    urls = ['<url><loc>https://easysvip.com/netdisk_share.html</loc>'
            f'<lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.9</priority></url>']
    seen = set()
    for m in movies:
        vid = m["vod_ids"][0]
        if vid in seen:
            continue
        seen.add(vid)
        urls.append(f'<url><loc>https://easysvip.com/index.php/vod/detail/id/{vid}.html</loc>'
                    f'<changefreq>weekly</changefreq><priority>0.7</priority></url>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>")


async def main():
    movies = await fetch_movies()
    html = render_html(movies)
    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")
    sitemap = OUTPUT.parent / "sitemap_netdisk.xml"
    sitemap.write_text(render_sitemap(movies), encoding="utf-8")
    print(f"✅ 公开页已生成: {OUTPUT} ({len(movies)} 部)")
    print(f"✅ Sitemap 已生成: {sitemap}（{len(movies)} 个详情页 + 汇总页）")


if __name__ == "__main__":
    asyncio.run(main())
