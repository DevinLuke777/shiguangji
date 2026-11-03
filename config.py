#!/usr/bin/env python3
"""
拾光集 · 统一配置管理
所有路径、环境变量、默认值集中在这里，其他模块统一从这里导入。
"""
import os
from pathlib import Path

# ── 项目根目录 ──────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()

# ── 数据库 ──────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "media_library.db"))

# ── 媒体根目录 ──────────────────────────────────────
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", str(BASE_DIR / "media"))

# ── 队列文件 ────────────────────────────────────────
QUEUE_FILE = os.environ.get("QUEUE_FILE", os.path.join(MEDIA_ROOT, "_queue.json"))
QUEUE_LOCK = os.path.join(MEDIA_ROOT, "_queue.lock")

# ── Worker 相关路径 ─────────────────────────────────
INGEST_DIR = os.environ.get("INGEST_DIR", str(BASE_DIR))
INGEST_SCRIPT = os.path.join(INGEST_DIR, "ingest.py")

WORKER_PYTHON = os.environ.get("WORKER_PYTHON", "python3")
XHS_PYTHON = os.environ.get("XHS_PYTHON", "")  # XHS-Downloader venv python 路径
XHS_DIR = os.environ.get("XHS_DIR", "")  # XHS-Downloader 项目目录
XHS_COOKIE_FILE = os.environ.get("XHS_COOKIE_FILE", "")

# ── 轻解析服务 ──────────────────────────────────────
LITE_PARSER_URL = os.environ.get("LITE_PARSER_URL", "http://localhost:8086")

# ── 代理设置（X/Twitter 下载用）─────────────────────
PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:20172")

# ── Bark 推送 ───────────────────────────────────────
BARK_URL = os.environ.get("BARK_URL", "")

# ── 分页 ────────────────────────────────────────────
ITEMS_PER_PAGE = int(os.environ.get("ITEMS_PER_PAGE", "40"))

# ── 日志 ────────────────────────────────────────────
LOG_DIR = os.environ.get("LOG_DIR", str(BASE_DIR / "logs"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# ── User-Agent ──────────────────────────────────────
UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# ── 文件扩展名 ──────────────────────────────────────
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# ── 静态文件目录 ────────────────────────────────────
STATIC_DIR = BASE_DIR / "static"

# ── 模板目录 ────────────────────────────────────────
TEMPLATE_DIR = BASE_DIR / "templates"
