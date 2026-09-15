# 🏛️ 拾光集（shiguangji）

自托管**内容收藏库**：把社交平台（小红书/抖音/微博/TikTok/X）的图文视频下载后自动归档，网页浏览/播放/搜索，支持作者筛选与双时间排序。

```
用户发链接 → 下载（轻解析/备用通道）→ 媒体文件存 NAS + 元数据入库
→ 网页卡片流浏览 / 视频播放 / 图片灯箱 / 搜索筛选
```

## ✨ 功能

- 🎴 瀑布流卡片（缩略图 + 平台标签 + 作者 + 时间）
- 🔍 搜索（标题/作者/内容）、平台筛选、**作者点击筛选**
- 🕐 双时间（发布 + 采集）+ 排序切换
- 🎬 视频内联播放、图片网格 + **Lightbox 灯箱**
- 👤 作者头像本地代理（防外链打乱布局）
- 🖼️ 自动缩略图（视频抽帧/图片缩放）
- 📥 **网页粘贴链接自动入库**：顶部输入框粘抖音/小红书链接 → 后台队列自动下载归档入库（详见 `download_worker.py`），零手动干预

## 📦 快速部署（Docker）

```bash
git clone https://github.com/DevinLuke777/shiguangji.git
cd shiguangji

# 1. 准备媒体目录（可选，先建空目录也行）
mkdir -p /path/to/your/media   # 之后按 平台/日期/标题 结构放内容

# 2. 改 docker-compose.yml 里的媒体路径
vim docker-compose.yml   # /path/to/your/media → 你的实际路径

# 3. 启动
docker compose up -d --build

# 4. 打开
# http://你的IP:8090
```

## 🔧 手动部署（不用 Docker）

```bash
pip install flask
python3 init_db.py          # 建库（Docker 方式会自动建，可跳过）
MEDIA_ROOT=/path/to/media python3 app.py   # 启动 Web
```

## ✅ 部署前须知（两条路，选你要的）

**A. 只要「网页媒体库」**（最小部署，3 步搞定）
1. `docker compose up -d --build`
2. 把已有媒体按 `平台/日期/标题/文件` 放进媒体目录
3. `python3 ingest.py --scan`（在宿主机跑，Python 3 + 标准库即可）扫描补录进库

不部署下面那层，网页照样能浏览、搜索、播放，只是不能「粘链接自动下载」。

**B. 再加「粘链接自动采集」**（进阶，需要额外自建服务）

| 组件 | 作用 | 依赖 |
|---|---|---|
| `download_worker.py` | 队列工人：下载 → 归档 → 入库 | Python 3 |
| 哨兵（`while true` 循环跑 `--drain`）或 cron 每分钟跑一次 | 自动触发 | — |
| 轻解析服务（`localhost:8086`） | 抖音解析 + 落盘 | **需自建**（本仓库不含） |
| XHS-Downloader | 小红书图文/视频兜底下载 | **需自建** + 小红书 cookie |
| 公网入口（可选 Cloudflare Tunnel） | 手机在外面也能提交链接 | — |

worker 的环境变量（都能覆盖脚本里的默认值）：`MEDIA_ROOT`（媒体根目录）、`INGEST_DIR`（ingest.py 所在目录）、`WORKER_PYTHON`、`XHS_PYTHON`（XHS-Downloader 的 venv python）、`XHS_DIR`、`XHS_COOKIE_FILE`（cookie 文件）、`QUEUE_FILE`。细节见下面「网页粘贴自动入库」一节。

> **最容易踩的坑**：Docker 部署时**不要把宿主空目录挂到 `/app`** —— 那会盖掉镜像里的 `app.py`，页面直接空白/404。数据库请挂 `/data`（见 `docker-compose.yml`）。

## 📥 内容入库

媒体目录结构要求：`媒体根目录/平台/日期/标题/文件`（与轻解析下载目录一致）

```bash
# 方式1：扫描目录自动补录（只有平台/标题/日期，无作者）
python3 ingest.py --scan

# 方式2：指定原链接入库（自动抓作者/标题/发布时间/原链接）
python3 ingest.py --platform 小红书 --url "https://xhslink.cn/xxx" --path "小红书/2026-08-08/标题"

# 生成缩略图（新内容入库后跑）
python3 gen_thumbs.py
```

## 🧠 元数据采集

| 平台 | 元数据源 | 媒体下载 |
|------|---------|---------|
| 小红书 | 页面 JSON（title/nickName/avatar/time）| 视频=309 流无水印；图文=ci.xiaohongshu.com 无水印通道 |
| 抖音 | 轻解析 API（需自建轻解析服务）| 轻解析 |
| 微博 | m.weibo.cn API | original 原图 + live 图 |
| TikTok | tikwm API | tikwm |
| X/推特 | yt-dlp | yt-dlp |

> 采集脚本是通用参考实现，各平台反爬策略变化快，可能需要按当时情况调整。

## ⚠️ 踩坑记录

1. 小红书 259 流带水印 → 必须抓 `_309.mp4` 流（**2026-09-14 实锤**：页面里流顺序是 `MINI_APP_259` 在前、`X265_MP4_WEB_309_h5` 在后，取「第一个 masterUrl」就会拿到水印版。判定法：本地文件字节数 == 259 流 `Content-Length` 即水印版）
2. 小红书图文 CDN 图带水印 → 用 XHS-Downloader（官方签名接口）取无水印原图
3. 图片条目必须生成 `_thumb.jpg`（否则卡片加载原图卡死）
4. 外链头像不能直接渲染（手机浏览器打碎布局）→ 走 `/avatar/<id>` 代理
5. 平台"X"标签显示为「推特」（避免误认关闭按钮）
6. Docker 内 `MEDIA_ROOT=/media`（挂载点），宿主机脚本用实际路径
7. `original_url` 不能存空串（UNIQUE 冲突）→ scan 用 `scan://路径` 占位
8. 本机服务（`localhost:8086` 轻解析）**不能走 http 代理**，否则被劫持返回 502 Bad Gateway → worker 的 `fetch()` 用 `ProxyHandler({})` 直连
9. 图文条目的图片用 `glob("Download/**/*.png")` 一次即可（递归已含根目录），再拼一次 `Download/*.png` 会导致每张图存两份

## 📥 网页粘贴自动入库（download_worker.py）

顶部输入框粘链接 → 写 `_queue.json` → 宿主机 worker（哨兵 3 秒轮询 / cron 兜底）处理：

- **抖音**：轻解析服务解析 + 落盘，三重串号防护（清旧 untitled + 时间戳快照认领 + 认领即改名），空标题/同名自动 `_2/_3` 后缀
- **小红书视频**：抓页面 → **优先 309 无水印流**（无 309 才退回 XHS-Downloader，最后才用 259）
- **小红书图文**：XHS-Downloader 取无水印原图（失败降级 sns-webpic 带水印图）

```bash
python3 download_worker.py --drain     # 手动跑一次队列
```

## 🗓️ 更新记录

- 🧩 **开源可部署性打磨**：compose 服务名统一 shiguangji、新增「部署前须知」（最小部署 vs 进阶采集两条路 + 挂载坑）、worker 关键路径全部支持环境变量覆盖（默认值不变）
**2026-09-14**
- 🐛 **修复小红书视频水印 bug**：worker 原来取页面第一个 `masterUrl`（= 259 水印流），改为优先 309/258 无水印流；页面只给 259 时退回 XHS-Downloader 签名接口；页面抓取增加 cookie 兜底
- 🐛 **修复图文图片每张存两份**：图片 glob 递归 + 根目录写了两遍，已去重
- 🐛 **修复 502 Bad Gateway**：`fetch()` 走本机 8086 轻解析时被 http 代理劫持，改为直连（`ProxyHandler({})`）
- 🐛 app.py：`local_path` 为空时列表页不再抛异常
- 🧹 存量治理：全库小红书视频按 259/309 流字节数核对，水印版批量换 309；64 条图文条目重复图 md5 去重 + 重新编号

**2026-08-23**
- 队列页失败条目「🔄 重试」按钮（`/queue-retry`）；首屏缩略图提速（媒体文件 10 分钟缓存 + 前 12 张高优先级）

## 📁 项目结构

```
app.py             # Web 应用（列表/详情/搜索/头像代理/自动建库/队列页）
ingest.py          # 入库脚本（抓元数据 / 扫描目录）
gen_thumbs.py      # 缩略图生成
init_db.py         # 手动建库
download_worker.py # 队列 worker（抖音/小红书自动下载归档入库）
Dockerfile         # 镜像（python:3.11-slim + flask）
docker-compose.yml
```

## 🔒 隐私说明

- 数据库、头像、媒体文件都在 `.gitignore` 中，**不会提交到仓库**
- 本仓库只含代码，不含任何收藏数据

## 📄 License

MIT
