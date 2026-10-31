#!/usr/bin/env python3
"""
拾光集 · 入库脚本（模块化重构版）
用法:
  python3 ingest.py --platform 抖音 --url <原链接> --path <本地相对路径>
  python3 ingest.py --scan   # 扫描媒体目录，按目录结构补录
自动从 URL 抓取元数据（作者/标题/内容/日期），失败可用 --title/--author 等覆盖。
"""
import argparse
import json
import os
import re
import urllib.request
import urllib.parse
from datetime import datetime
from config import DB_PATH, MEDIA_ROOT, LITE_PARSER_URL, BARK_URL, UA_MOBILE
from db import get_conn, insert_item, get_item_by_url, item_exists_by_path
from utils import parse_xhs_meta, download_avatar_local
from logger import log


def fetch(url: str, timeout: int = 20, referer: str = None, extra_headers: dict = None) -> str:
    """HTTP GET 请求（返回文本）"""
    headers = {"User-Agent": UA_MOBILE}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


# ─── 元数据抓取 ──────────────────────────────────────

def get_meta_xiaohongshu(url: str) -> dict:
    """小红书：页面 JSON 抓 title/author/desc/time（复用 utils.parse_xhs_meta）"""
    html = fetch(url)
    meta = parse_xhs_meta(html)
    return {
        "title": meta["title"][:100] if meta["title"] else None,
        "author_name": meta["author_name"],
        "author_avatar": meta["author_avatar"],
        "content": meta["content"],
        "post_time": meta["post_time"],
    }


def get_meta_douyin(url: str) -> dict:
    """抖音：走轻解析 API 拿 title/author"""
    try:
        enc = urllib.parse.quote(url, safe="")
        api = f"{LITE_PARSER_URL}/video/share/url/parse?url={enc}"
        d = json.loads(fetch(api, timeout=30))
        data = d.get("data") or {}
        author = (data.get("author") or {}).get("name")
        avatar = (data.get("author") or {}).get("avatar")
        return {"title": data.get("title"), "author_name": author, "author_avatar": avatar}
    except Exception:
        return {}


def get_meta_weibo(url: str) -> dict:
    """微博：m.weibo.cn API"""
    m = re.search(r"weibo\.com/\d+/(\w+)", url)
    if not m:
        return {}
    try:
        api = f"https://m.weibo.cn/statuses/show?id={m.group(1)}"
        txt = fetch(api, timeout=20, referer="https://m.weibo.cn/",
                    extra_headers={"X-Requested-With": "XMLHttpRequest"})
        d = json.loads(txt)
        data = d.get("data") or {}
        user = data.get("user") or {}
        text = re.sub(r"<[^>]+>", "", data.get("text", ""))[:500]
        post_time = None
        ca = data.get("created_at")
        if ca:
            try:
                post_time = datetime.strptime(ca, "%a %b %d %H:%M:%S %z %Y").strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return {"title": text[:50], "content": text,
                "author_name": user.get("screen_name"), "author_avatar": user.get("avatar_hd") or user.get("avatar_large"),
                "post_time": post_time}
    except Exception:
        return {}


def get_meta_tiktok(url: str) -> dict:
    """TikTok：tikwm API"""
    try:
        api = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url, safe='')}"
        d = json.loads(fetch(api, timeout=25))
        if d.get("code") == 0:
            data = d.get("data") or {}
            author = data.get("author") or {}
            post_time = None
            ct = data.get("create_time")
            if ct:
                post_time = datetime.fromtimestamp(int(ct)).strftime("%Y-%m-%d %H:%M:%S")
            return {"title": data.get("title") or data.get("content_desc"),
                    "author_name": author.get("nickname") or author.get("unique_id"),
                    "author_avatar": author.get("avatar") or data.get("origin_cover"),
                    "post_time": post_time}
    except Exception:
        pass
    return {}


def get_meta_x(url: str) -> dict:
    """X/Twitter：yt-dlp --dump-json 元数据"""
    import subprocess
    try:
        r = subprocess.run(["yt-dlp", "--dump-json", "--no-warnings", url],
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PATH": os.environ.get("PATH", "")})
        d = json.loads(r.stdout)
        title = (d.get("title") or "").split(" - ", 1)[-1]
        return {"title": title[:100], "author_name": d.get("uploader"),
                "author_avatar": d.get("thumbnail"), "content": None}
    except Exception:
        return {}


META_FUNCS = {
    "小红书": get_meta_xiaohongshu,
    "抖音": get_meta_douyin,
    "微博": get_meta_weibo,
    "TikTok": get_meta_tiktok,
    "X": get_meta_x,
    "Twitter": get_meta_x,
}


def bark_notify(row: dict) -> None:
    """新收藏通知（Bark）"""
    if not BARK_URL:
        return
    title = f"📥 新收藏 [{row['platform']}]"
    body = row.get("title") or "无标题"
    if row.get("author_name"):
        body += f"\n👤 {row['author_name']}"
    if row.get("post_date"):
        body += f"\n🕐 {row['post_date']}"
    try:
        u = f"{BARK_URL.rstrip('/')}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
        urllib.request.urlopen(urllib.request.Request(u), timeout=8).read()
    except Exception as e:
        log.warning(f"Bark 推送失败: {e}")


def detect_media_type(local_path: str) -> str:
    """根据目录内容判断 media_type"""
    full = os.path.join(MEDIA_ROOT, local_path)
    if not os.path.isdir(full):
        return "image"
    for f in os.listdir(full):
        if f.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
            return "video"
    return "image"


def detect_cover_path(local_path: str) -> str:
    """检测封面路径（_thumb.jpg 优先，否则第一张图）"""
    full = os.path.join(MEDIA_ROOT, local_path)
    if not os.path.isdir(full):
        return ""
    thumb = os.path.join(local_path, "_thumb.jpg").replace("\\", "/")
    if os.path.isfile(os.path.join(full, "_thumb.jpg")):
        return thumb
    for f in sorted(os.listdir(full)):
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) and f != "_thumb.jpg":
            return os.path.join(local_path, f).replace("\\", "/")
    return ""


def ingest(platform: str, url: str, local_path: str,
           title: str = None, author: str = None, avatar: str = None,
           content: str = None, post_date: str = None, force: bool = False,
           src_tag: str = "") -> bool:
    """入库主函数"""
    meta = {}
    fn = META_FUNCS.get(platform)
    if fn:
        try:
            meta = fn(url) or {}
        except Exception as e:
            log.warning(f"元数据抓取失败 [{platform}] {url}: {e}")

    row = {
        "platform": platform,
        "title": title or meta.get("title"),
        "content": content or meta.get("content"),
        "author_name": author or meta.get("author_name"),
        "author_avatar": avatar or meta.get("author_avatar"),
        "post_date": post_date or meta.get("post_time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "original_url": url,
        "local_path": local_path,
        "cover_path": detect_cover_path(local_path),
        "media_type": detect_media_type(local_path),
        "src_tag": src_tag or "",
        # 309/258 是平台无水印流 -> 直接判 ok；259 是水印流 -> 可疑；其余留空待检测
        "wm_flag": ("ok" if src_tag in ("309", "258") else ("suspect" if src_tag == "259" else "")),
    }
    if not row["title"] and local_path:
        row["title"] = os.path.basename(local_path.rstrip("/"))[:100]

    # 去重保护
    if url and not force:
        dup = get_item_by_url(url)
        if dup:
            log.info(f"⚠️ 已收藏过 (id={dup['id']})「{dup.get('title') or '无标题'}」，跳过")
            return False

    ok = insert_item(row)
    if ok:
        log.info(f"✅ 入库: [{platform}] {row['title'] or '无标题'} 作者:{row['author_name'] or '?'}")
        bark_notify(row)
        # 头像本地化（让 avatar 路由命中本地文件，减少外链依赖）
        avatar_url = row.get("author_avatar")
        if avatar_url:
            try:
                from db import get_conn
                conn = get_conn()
                item_id = conn.execute("SELECT id FROM items WHERE original_url=?", (url,)).fetchone()
                conn.close()
                if item_id:
                    avatars_dir = os.path.join(os.path.dirname(DB_PATH), "avatars")
                    download_avatar_local(avatar_url, os.path.join(avatars_dir, f"{item_id[0]}.jpg"))
            except Exception as e:
                log.warning(f"头像本地化失败: {e}")
    else:
        log.error(f"❌ 入库失败: [{platform}] {row['title']}")
    return ok


def scan_and_ingest() -> None:
    """扫描媒体目录：平台/日期/标题/文件 → 入库"""
    if not os.path.isdir(MEDIA_ROOT):
        log.error(f"媒体目录不存在: {MEDIA_ROOT}")
        return
    for platform in os.listdir(MEDIA_ROOT):
        pdir = os.path.join(MEDIA_ROOT, platform)
        if not os.path.isdir(pdir) or platform.startswith("."):
            continue
        for date_dir in os.listdir(pdir):
            ddir = os.path.join(pdir, date_dir)
            if not os.path.isdir(ddir):
                continue
            for item_dir in os.listdir(ddir):
                idir = os.path.join(ddir, item_dir)
                if not os.path.isdir(idir):
                    continue
                files = [f for f in os.listdir(idir) if not f.startswith(".")]
                if not files:
                    continue
                rel = f"{platform}/{date_dir}/{item_dir}"
                if item_exists_by_path(rel):
                    continue
                row = {
                    "platform": platform,
                    "title": item_dir,
                    "author_name": None,
                    "post_date": date_dir,
                    "local_path": rel,
                    "original_url": f"scan://{rel}",
                    "cover_path": detect_cover_path(rel),
                    "media_type": detect_media_type(rel),
                }
                ok = insert_item(row)
                if ok:
                    log.info(f"  ✅ 入库: [{platform}] {item_dir}")
    log.info("扫描完成")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform")
    ap.add_argument("--url")
    ap.add_argument("--path")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--avatar")
    ap.add_argument("--content")
    ap.add_argument("--date")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--force", action="store_true", help="重复链接时强制覆盖更新")
    args = ap.parse_args()
    if args.scan:
        scan_and_ingest()
    elif args.platform and args.url:
        ingest(args.platform, args.url, args.path, args.title, args.author, args.avatar, args.content, args.date, args.force)
    else:
        print("用法: python3 ingest.py --platform 抖音 --url <链接> --path <相对路径> [--title ...]")
        print("      python3 ingest.py --scan")
        print("      python3 ingest.py --platform 小红书 --url <链接> --path <路径> --force   # 强制更新重复项")
