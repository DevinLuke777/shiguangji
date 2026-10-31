# 拾光集 · 重构迁移说明

## 改了什么

### 架构层面
- **拆分巨石 app.py（950行）** → 模块化结构
  - `config.py` — 统一配置（所有路径/环境变量集中）
  - `db.py` — 数据库层（封装所有 SQLite 操作 + 连接池）
  - `utils.py` — 公共工具（clean_title/fetch/pick_stream/safe_media_path）
  - `queue_manager.py` — 队列管理（带 fcntl 文件锁防竞态）
  - `logger.py` — 统一日志（RotatingFileHandler + 控制台）
  - `templates/*.html` — Jinja2 模板（base/index/detail/queue/stats）
  - `app.py` — 只剩路由（200行）

### 性能
- **消除 N+1 查询**：列表页不再对每个 item 调 `os.listdir()`
  - 新增 `cover_path`、`media_type` 字段，入库时自动填充
  - 列表页纯 SQL 查询，不再碰文件系统
- **分页**：`/` 路由支持 `?page=N`，默认 40 条/页
- **数据库索引**：新增 `idx_author`、`idx_media_type`
- **WAL 模式**：并发读写不再阻塞

### 部署
- **消除全部硬编码路径**：`/vol1/1000/...`、`/home/<user>/...` 全部消除
  - 统一走 `config.py` 的环境变量
  - 新增 `.env.example` 配置模板
- **Dockerfile 补 ffmpeg**：缩略图生成不再依赖宿主机
- **docker-compose 加环境变量**

### 代码质量
- **类型标注**：核心函数全部加 typing
- **异常处理**：`except Exception: pass` → 至少 log 记录
- **去重**：`fetch()`、`clean_title()`、`pick_stream()` 统一到 utils.py
- **路径安全**：`safe_media_path()` 统一防穿越校验
- **24 个单元测试**：`tests/test_core.py` + `tests/test_db.py` 全部通过

### 队列
- **文件锁保护**：`queue_manager.py` 用 `fcntl.flock` 保护队列读写
- **清理逻辑独立**：`cleanup_queue()` 函数复用

### 缩略图
- **并行生成**：`python3 gen_thumbs.py -j 4`（4线程并行）
- **--update-db**：生成时同步更新数据库 cover_path

---

## 怎么从旧版迁移

### 1. 备份
```bash
cp media_library.db media_library.db.bak
```

### 2. 替换代码
把新代码覆盖到原项目目录。

### 3. 配置环境变量
```bash
cp .env.example .env
vim .env  # 改成你的实际路径
```

关键变量：
```bash
MEDIA_ROOT=/your/media/path        # 媒体根目录
DB_PATH=/your/db/media_library.db  # 数据库路径
LITE_PARSER_URL=http://localhost:8086  # 轻解析服务
XHS_PYTHON=/your/xhs-venv/bin/python    # 小红书下载器
XHS_COOKIE_FILE=/your/cookie.txt        # 小红书 cookie
```

### 4. 数据库自动迁移
启动 app.py 时会自动执行 `migrate_db()`，为旧表添加：
- `cover_path TEXT DEFAULT ''` — 封面图路径（消除 N+1）
- `media_type TEXT DEFAULT 'image'` — 媒体类型

### 5. 补全已有条目的 cover_path
```bash
python3 gen_thumbs.py --update-db -j 4
```

### 6. 启动
```bash
python3 app.py
# 或 Docker
docker compose up -d --build
```

---

## 新文件结构

```
shiguangji/
├── config.py           # 统一配置（环境变量集中）
├── logger.py           # 日志模块（轮转文件 + 控制台）
├── db.py               # 数据库层（封装 + 连接池 + 迁移）
├── utils.py            # 公共工具（去重后的 fetch/clean_title/pick_stream）
├── queue_manager.py    # 队列管理（带文件锁）
├── app.py              # Flask 路由（只剩路由逻辑）
├── ingest.py           # 入库脚本（用 db 层 + 自动 cover_path/media_type）
├── download_worker.py  # 队列 worker（用统一配置 + db 层）
├── gen_thumbs.py       # 缩略图（并行 + --update-db）
├── init_db.py          # 建库（一行调用）
├── templates/          # Jinja2 模板
│   ├── base.html       # 基础布局 + CSS 变量
│   ├── index.html      # 列表页（瀑布流 + 分页）
│   ├── detail.html     # 详情页
│   ├── queue.html      # 队列页
│   └── stats.html      # 统计页
├── tests/              # 单元测试
│   ├── test_core.py    # 核心函数测试（19个）
│   └── test_db.py      # 数据库层测试（5个）
├── requirements.txt    # Python 依赖
├── Dockerfile          # 含 ffmpeg
├── docker-compose.yml
├── .env.example        # 配置模板
└── .gitignore
```

---

## 跑测试
```bash
python -m unittest discover -s tests -v
# 31 个测试全过
```

---

## 第二轮优化（bug 修复 + 工程化）

### 真 bug 修复
- **`download_weibo()` 变量未定义 bug** — 清理 lite.parser 残留段引用了未赋值的 `rel`，已把 `rel` 赋值提到清理逻辑之前（原代码继承 bug，被 except 吞掉一直没发现）

### 死代码清理
- 删除 `utils.list_media_files()`（无人调用）和 `format_file_size()`（无人调用）
- 新增 `utils.list_item_files()`（detail 路由专用，带自然排序），detail 路由已改用它

### 架构债清理
- **数据库版本化迁移** — `db.migrate_db()` 改用 `schema_version` 表，按版本号顺序执行：
  - v1: 添加 cover_path/media_type 字段
  - v2: 补全空 cover_path（用第一张图填充）
  - 以后新增迁移只需加 `if current < N` 分支
- **头像缓存 LRU + TTL** — 新增 `utils.AvatarCache`（max_size=200, ttl=3600s），替代无上限字典。作者换头像 1 小时后自动刷新
- **stats 走 db 层** — 新增 `db.get_stats()`，stats 路由不再裸写 SQL
- **小红书元数据解析去重** — 抽到 `utils.parse_xhs_meta()`，worker 和 ingest 共用

### worker 增强
- **自动重试** — 失败自动改回 pending 重试（最多 2 次），第 3 次才标记 failed
- **失败 Bark 通知** — 超过重试次数后推送 Bark（复用 BARK_URL 配置）

### 体验/工程
- **CSS 拆独立文件** — `static/css/main.css`，改样式不用碰 HTML
- **404/500 错误页** — 新增 `templates/error.html`
- **gen_thumbs 用进程池** — `ProcessPoolExecutor` 替代线程池（ffmpeg CPU 密集，绕过 GIL）
- **GitHub Actions CI** — `.github/workflows/tests.yml`，push 自动跑语法检查 + 测试
- **新增 7 个测试** — parse_xhs_meta（3个）+ AvatarCache LRU/TTL（4个），总计 31 个
