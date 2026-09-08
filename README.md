# 网盘链接自动转存服务

将全网搜到的夸克分享链接自动转存到自己的网盘，通过**真 Chrome 可信事件 UI 自动化**生成自己的分享链接，写入数据库供网站详情页展示，赚取转存推广收益。

> 📄 可视化介绍：打开 [project_intro.html](project_intro.html) 查看架构图、流程图与风控攻防全记录

## 系统架构

```
本地 Mac
├── Docker MySQL (127.0.0.1)      ← 任务队列 5000+ 部影片
├── PanSou Docker (多源搜索)       ← 远程TG → 插件 → 本地 自动降级
├── 转存调度器 (Python)            ← 夸克 REST API 转存
├── CDP 真 Chrome (端口9223)       ← 可信鼠标事件 UI 分享
└── 实时同步 → 线上生产库 (194.41.36.57)
                    ↓
        网站详情页 get_my_netdisk 接口
        优先展示自己的链接，无链接时降级全网搜
```

## 核心突破：分享存活率 0% → 100%

夸克 WAF（baxia/fireyejs）会拦截一切程序特征创建的分享：

| 创建方式 | 结局 |
|----------|------|
| 裸 API fetch（aiohttp，即使带全套浏览器头） | ❌ 审计下架 |
| 页面上下文 fetch（真实 Chrome 会话） | ❌ 审计下架 |
| JS el.click() 驱动 UI（isTrusted=false） | ❌ 审计下架 |
| **Playwright page.mouse（isTrusted=true）+ 拟人轨迹** | **✅ 存活** |

唯一存活路径：**CDP 接管真 Chrome，用真实输入管线事件（拟人鼠标轨迹）完整走一遍 UI 流程**。

## 快速开始

```bash
# 1. 环境
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. 本地 Docker MySQL
docker run -d --name mysql-easysvip \
  -e MYSQL_ROOT_PASSWORD=easysvip123 -e MYSQL_DATABASE=easysvip \
  -p 3306:3306 mysql:8.0 \
  --character-set-server=utf8mb4 --max_allowed_packet=512M

# 3. 配置线上库连接（同步目标）
cp db_online.ini.example db_online.ini   # 填入线上库地址/账号/密码
# （此文件已被 .gitignore 排除，不会提交）

# 4. 建表 + 初始化任务（从 mac_vod 提取今年院线电影）
python init_db.py
python task_flow.py init
python task_flow.py status

# 5. 提取夸克 Cookie（Chrome 先登录 pan.quark.cn）
python extract_cookies.py

# 6. 启动常驻运行器
nohup python batch_runner.py > runner.log 2>&1 &
```

## 常用命令

```bash
python task_flow.py status       # 任务状态统计
python task_flow.py init         # 从 mac_vod 初始化/补充任务
python sync_push.py              # 手动推送本地增量到线上库
python cleanup_dups.py           # 预览/清理网盘重复文件（--apply 执行）
python daily_report.py           # 手动生成当日转存报告
python add_account.py add quark 13800138000 pwd   # 添加账号
```

## 优先级策略

| 级别 | 范围 | 说明 |
|------|------|------|
| P300 | 2026 中国大陆/香港院线（60天内上映） | 最高优先 |
| P200 | 2026 海外院线 | 次优先 |
| P100 | 2025/2026 其他电影 | 常规 |
| P10 | 纪录片/体育/游戏等杂质 | 降级 |

## 风控对策速查

| 风控 | 对策 |
|------|------|
| TLS 指纹检测（aiohttp 被 401） | 分享走 CDP 真 Chrome |
| 合成事件检测（isTrusted=false） | page.mouse 可信事件 + 拟人轨迹 |
| 内容审核下架（audit_status=4） | 每轮匿名审计巡检 + 死链自动重置重分享 |
| 混淆文件名（南z京z照z相z馆） | 转存记录 fid + CJK 字符提取匹配 |
| Cookie 轮换（__puus） | 捕获 Set-Cookie 手动更新 |
| 无头浏览器检测 | 有头模式 + 反检测脚本注入 |

## 项目结构

```
batch_runner.py      常驻运行器（探针+审计巡检+分轮调度+日报）
task_flow.py         单任务全流程（搜索→转存→分享→入库→同步）
quark_api.py         夸克 REST API（stoken/列文件/转存/任务轮询）
browser_share.py     ★ 可信事件 UI 分享（核心模块）
pansou_client.py     多端点搜索降级 + 轮询
sync_push.py         本地→线上增量同步（游标断点续传）
audit                审计巡检（task_flow.audit_and_reset_completed）
daily_report.py      每日转存报告（reports/）
reconcile.py         分享对账找回
cleanup_dups.py      网盘重复文件清理
extract_cookies.py   Chrome Cookie 提取
init_db.py / add_account.py / db_watchdog.py / config.py
```

## 数据库

| 表 | 位置 | 职责 |
|----|------|------|
| quark_transfer | 本地 | 任务队列（priority/release_date/quark_fid/my_url/status） |
| quark_link_vod | 本地 | 链接 ↔ vod_id 一对多映射 |
| mac_vod_netdisk | 本地 + 线上 | 网盘资源（网站读取的表） |
| cloud_accounts | 本地 | 账号池（多账号扩展位） |

## 注意事项

1. **db_online.ini 含线上库密码，已被 .gitignore 排除，切勿提交**
2. 自动化窗口是独立 Chrome 实例（/tmp/chrome_cdp_profile），不要手动操作它，最小化即可
3. 批量运行期间避免同账号高频手动转存
4. 夸克网页结构变化时检查 `screenshots/` 与 `runner.log`
