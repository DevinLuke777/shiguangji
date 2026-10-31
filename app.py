#!/usr/bin/env python3
"""
拾光集 · Web 展示应用（模块化重构版）
列表页：瀑布流卡片 + 分页 + 搜索/筛选/排序
详情页：标题 + 作者信息条 + 正文 + 视频播放 + 图片灯箱
队列页：粘贴链接自动入库状态管理
统计页：平台分布 / 作者Top / 收藏趋势
"""
import os
import re
import json
import time
import urllib.request
from datetime import datetime
from flask import (
    Flask, render_template, request, abort, url_for,
    Response, redirect, jsonify, send_file
)
from config import (
    DB_PATH, MEDIA_ROOT, STATIC_DIR, ITEMS_PER_PAGE
)
from db import (
    init_db, migrate_db, ensure_indexes, query_items, get_item, get_item_by_url,
    delete_item as db_delete_item, get_platforms, get_authors, get_stats
)
from queue_manager import (
    load_queue, save_queue, add_to_queue, retry_queue_item,
    clear_queue, cleanup_queue
)
from utils import list_item_files, safe_media_path, platform_label, AvatarCache
from logger import log

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ─── 缓存 ───────────────────────────────────────────
_avatar_cache = AvatarCache(max_size=200, ttl=3600)  # LRU + 1小时 TTL


# ─── 启动初始化 ──────────────────────────────────────
def ensure_db():
    """启动时自动建库 + 迁移"""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    init_db()       # 建表（若不存在）
    migrate_db()    # 旧库补列（必须在建新字段索引之前）
    ensure_indexes()  # 补建依赖新字段的索引


# ─── 全局缓存控制 ──────────────────────────────────────
@app.after_request
def no_cache(resp):
    """页面禁缓存；媒体/静态文件 10 分钟缓存"""
    p = request.path
    if p.startswith("/media/") or p.startswith("/avatar/") or p.startswith("/static/"):
        resp.headers["Cache-Control"] = "public, max-age=600"
    else:
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ─── 静态文件路由 ──────────────────────────────────────
@app.route("/favicon.ico")
def favicon_ico():
    p = os.path.join(STATIC_DIR, "favicon.ico")
    if not os.path.exists(p):
        return Response(status=404)
    with open(p, "rb") as fh:
        return Response(fh.read(), mimetype="image/vnd.microsoft.icon")


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    p = os.path.join(STATIC_DIR, "favicon.png")
    if not os.path.exists(p):
        return Response(status=404)
    with open(p, "rb") as fh:
        return Response(fh.read(), mimetype="image/png")


# ─── 列表页 ──────────────────────────────────────────
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    plat = request.args.get("platform", "").strip()
    author = request.args.get("author", "").strip()
    sort = request.args.get("sort", "created")
    mode = request.args.get("mode", "")
    page = request.args.get("page", 1, type=int)
    per_page = ITEMS_PER_PAGE

    media = request.args.get("media", "").strip()
    items, total = query_items(
        q=q, platform=plat, author=author, media=media, sort=sort,
        page=page, per_page=per_page
    )
    platforms = get_platforms()

    # 补充卡片数据
    card_items = []
    for idx, it in enumerate(items):
        it["idx"] = idx
        card_items.append(it)

    # URL 构建
    from urllib.parse import urlencode
    base_args = {}
    if q:
        base_args["q"] = q
    if sort:
        base_args["sort"] = sort

    url_no_plat = "/?" + urlencode(base_args)
    created_args = dict(base_args, sort="created")
    posted_args = dict(base_args, sort="posted")

    total_pages = (total + per_page - 1) // per_page
    prev_args = dict(base_args, page=page - 1) if page > 1 else {}
    next_args = dict(base_args, page=page + 1) if page < total_pages else {}

    return render_template("index.html",
        items=card_items,
        columns=[card_items[0::2], card_items[1::2]],
        platforms=platforms, q=q, platform=plat,
        total=total, author=author, sort=sort, mode=mode,
        page=page, per_page=per_page, total_pages=total_pages,
        url_no_plat=url_no_plat,
        url_created="/?" + urlencode(created_args),
        url_posted="/?" + urlencode(posted_args),
        url_prev="/?" + urlencode(prev_args) if prev_args else "",
        url_next="/?" + urlencode(next_args) if next_args else "",
        active_tab="manage" if mode == "manage" else "index",
    )


# ─── 详情页 ──────────────────────────────────────────
@app.route("/item/<int:item_id>")
def detail(item_id):
    it = get_item(item_id)
    if not it:
        abort(404)
    files = list_item_files(it.get("local_path", ""))
    return render_template("detail.html", it=it, files=files)


# ─── 头像代理 ──────────────────────────────────────────
@app.route("/avatar/<int:item_id>")
def avatar(item_id):
    local_avatar = os.path.join(os.path.dirname(DB_PATH), "avatars", f"{item_id}.jpg")
    if os.path.isfile(local_avatar):
        return send_file(local_avatar)

    it = get_item(item_id)
    if not it or not it.get("author_avatar"):
        abort(404)

    url = it["author_avatar"]
    cached = _avatar_cache.get(url)
    if cached:
        return Response(cached, content_type="image/jpeg")

    referer = "https://weibo.com/"
    if "xhscdn" in url:
        referer = "https://www.xiaohongshu.com/"
    elif "douyinpic" in url or "douyin" in url:
        referer = "https://www.douyin.com/"
    elif "twimg" in url:
        referer = "https://x.com/"
    elif "tiktokcdn" in url:
        referer = "https://www.tiktok.com/"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1",
            "Referer": referer,
        })
        data = urllib.request.urlopen(req, timeout=10).read()
        if len(data) > 2000000:
            abort(404)
        _avatar_cache.set(url, data)
        return Response(data, content_type="image/jpeg")
    except Exception:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=10).read()
            if len(data) > 2000000:
                abort(404)
            _avatar_cache.set(url, data)
            return Response(data, content_type="image/jpeg")
        except Exception:
            abort(404)


# ─── 媒体文件服务 ──────────────────────────────────────
@app.route("/media/<path:relpath>")
def media(relpath):
    try:
        full = safe_media_path(relpath)
    except ValueError:
        abort(403)
    if not os.path.isfile(full):
        abort(404)
    return send_file(full)


# ─── 删除 ──────────────────────────────────────────
@app.route("/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    rel_path = db_delete_item(item_id)
    if rel_path:
        try:
            d = safe_media_path(rel_path)
            if os.path.isdir(d):
                import shutil
                shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            log.warning(f"删除文件失败 {rel_path}: {e}")
    return redirect(url_for("index"))


@app.route("/delete-batch", methods=["POST"])
def delete_batch():
    import shutil
    ids = request.form.getlist("ids")
    for i in ids:
        rel_path = db_delete_item(int(i))
        if rel_path:
            try:
                d = safe_media_path(rel_path)
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    return redirect(url_for("index"))


# ─── 队列 ──────────────────────────────────────────
@app.route("/add", methods=["POST"])
def add_link():
    raw = request.form.get("links", "").strip()
    links = [l.strip() for l in re.split(r"[\s,]+", raw) if l.strip().startswith("http")]
    if not links:
        return redirect(url_for("index", msg="no_link"))
    added = 0
    for link in links:
        result = add_to_queue(link)
        if result.get("ok"):
            added += 1
    return redirect(url_for("index", msg="added" if added else "dup"))


@app.route("/api/add", methods=["GET", "POST"])
def api_add():
    """iOS 快捷指令 / 自动化接口"""
    raw = " ".join(x for x in (
        request.values.get("url"),
        request.values.get("links"),
        request.values.get("text"),
    ) if x)
    if not raw:
        raw = request.get_data(as_text=True)[:4000]

    links, seen_links = [], set()
    for cand in re.findall(r"https?://[^\s一-鿿,，、；;：:）)】\]]+", raw):
        cand = cand.rstrip(".,;:!?，。；：！？\"'}])>»\\")
        if cand and cand not in seen_links:
            seen_links.add(cand)
            links.append(cand)

    if not links:
        return _api_reply({"ok": False, "added": 0, "dup": 0, "msg": "没找到链接"})

    added, dup = [], []
    for link in links:
        result = add_to_queue(link)
        if result.get("ok"):
            added.append(link)
        else:
            dup.append(link)

    msg = f"已加入队列 {len(added)} 条" if added else "已在队列中(跳过)"
    if dup and added:
        msg += f"，{len(dup)} 条重复"
    return _api_reply({"ok": True, "added": len(added), "dup": len(dup), "msg": msg})


def _api_reply(payload, raw=""):
    if (request.values.get("fmt") or "").lower() == "json":
        return jsonify(payload)
    return Response(payload["msg"] + "\n", mimetype="text/plain; charset=utf-8")


@app.route("/api/live-photo/<int:item_id>")
def api_live_photo(item_id):
    """返回某条目的 Live 图文件 URL 对（jpg + mov）。
    供 iOS 快捷指令调用：下载 jpg+mov → 创建 Live Photo → 存相册。"""
    it = get_item(item_id)
    if not it:
        return jsonify({"ok": False, "msg": "条目不存在"}), 404
    rel = it.get("local_path", "")
    if not rel:
        return jsonify({"ok": False, "msg": "无本地路径"})
    try:
        d = safe_media_path(rel)
    except ValueError:
        return jsonify({"ok": False, "msg": "路径非法"}), 403
    if not os.path.isdir(d):
        return jsonify({"ok": False, "msg": "目录不存在"})

    # 收集图片和视频，按 basename 配对
    images = {}  # basename -> rel_path
    videos = {}  # basename -> rel_path
    for f in sorted(os.listdir(d)):
        if f.startswith(".") or f == "_thumb.jpg":
            continue
        low = f.lower()
        bname = f.rsplit(".", 1)[0]
        rel_f = os.path.join(rel, f).replace("\\", "/")
        if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            images[bname] = rel_f
        elif low.endswith((".mov", ".mp4")):
            videos[bname] = rel_f

    # 配对：basename 相同的 jpg+mov = Live Photo
    pairs = []
    for bname, img_rel in images.items():
        if bname in videos:
            pairs.append({
                "image": url_for("media", relpath=img_rel, _external=True),
                "video": url_for("media", relpath=videos[bname], _external=True),
                "basename": bname,
            })

    return jsonify({
        "ok": True,
        "item_id": item_id,
        "title": it.get("title", ""),
        "platform": it.get("platform", ""),
        "live_photos": pairs,
        "count": len(pairs),
    })


@app.route("/queue-clear-failed", methods=["POST"])
def queue_clear_failed():
    q = [it for it in load_queue() if it.get("status") != "failed"]
    save_queue(q)
    return redirect(url_for("queue_status"))


@app.route("/queue-clear-all", methods=["POST"])
def queue_clear_all():
    clear_queue()
    return redirect(url_for("queue_status"))


@app.route("/queue-retry", methods=["POST"])
def queue_retry():
    qid = request.form.get("id", type=int)
    if qid is not None:
        retry_queue_item(qid)
    return redirect(url_for("queue_status"))


@app.route("/queue")
def queue_status():
    q = load_queue()
    fail_count = sum(1 for it in q if it.get("status") == "failed")
    _, total = query_items(per_page=1)
    return render_template("queue.html", q=q, total=total, fail_count=fail_count, active_tab="queue")


# ─── 统计 ──────────────────────────────────────────
@app.route("/stats")
def stats():
    data = get_stats()
    platforms = data["platforms"]
    authors = data["authors"]
    trend = data["trend"]
    total = data["total"]
    max_p = max((r["c"] for r in platforms), default=1)
    max_a = max((r["c"] for r in authors), default=1)
    return render_template("stats.html",
        platforms=platforms, authors=authors, trend=trend,
        total=total, max_p=max_p, max_a=max_a, active_tab="stats",
    )


# ─── 错误页 ──────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="页面不存在"), 404


@app.errorhandler(500)
def server_error(e):
    log.error(f"500 错误: {e}")
    return render_template("error.html", code=500, message="服务器内部错误"), 500


# ─── 启动 ──────────────────────────────────────────
if __name__ == "__main__":
    ensure_db()
    log.info("拾光集启动: http://0.0.0.0:8090")
    app.run(host="0.0.0.0", port=8090)
