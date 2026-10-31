#!/usr/bin/env python3
"""
拾光集 · 后台自动入库队列 worker（模块化重构版）
从 _queue.json 读取 pending 链接，自动:
  抖音    -> 轻解析(8086) 解析下载 -> 归档 -> ingest
  小红书视频 -> 短链展开 -> 挖 309/258 无水印流 -> 下载 -> 归档 -> ingest
  小红书图文 -> XHS-Downloader + cookie 下无水印原图 -> 归档 -> ingest
               (token 过期则降级 sns-webpic 带水印渲染图并标记 degraded)
用法: python3 download_worker.py --drain    # 处理一次队列里所有 pending
用 cron 每 2 分钟调用一次。
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import List

from config import (
    MEDIA_ROOT, QUEUE_FILE, QUEUE_LOCK, INGEST_SCRIPT,
    WORKER_PYTHON, XHS_PYTHON, XHS_DIR, XHS_COOKIE_FILE,
    LITE_PARSER_URL, PROXY_URL, UA_MOBILE, UA_DESKTOP, BARK_URL,
)
from queue_manager import load_queue, save_queue
from utils import clean_title, sanitize_filename, gen_thumb_ffmpeg, pick_stream, parse_xhs_meta
from logger import log

MEDIA = MEDIA_ROOT
SRC_TAG = ""   # 当前任务的素材来源标签（309/258=无水印流 259=水印流 orig=原图 render=带水印渲染 wm=带水印 ok=干净）
QUEUE = QUEUE_FILE
LOCK = QUEUE_LOCK
INGEST = INGEST_SCRIPT
PY = WORKER_PYTHON
XHS_VENV = XHS_PYTHON

UA_I = UA_MOBILE
UA_D = UA_DESKTOP

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WARN = []


def fetch(url, timeout=20, referer=None):
    """HTTP 请求（不走代理）"""
    headers = {"User-Agent": UA_I}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    return _OPENER.open(req, timeout=timeout).read()


def today():
    return time.strftime("%Y-%m-%d")


def gen_thumb(d, title, is_video):
    """生成 _thumb.jpg：定位源文件（video 找 {title}.mp4，image 找第一张图）后调 ffmpeg"""
    src = os.path.join(d, f"{title}.mp4") if is_video else os.path.join(d, f"{title}_1.png")
    if not os.path.isfile(src):
        cands = sorted(glob.glob(os.path.join(d, "*.png")) + glob.glob(os.path.join(d, "*.jpg")))
        if not cands:
            return
        src = cands[0]
    gen_thumb_ffmpeg(src, os.path.join(d, "_thumb.jpg"), is_image=not is_video)


# ── 抖音 ──────────────────────────────────────────────
def download_douyin(url):
    """轻解析解析+下载，返回 (relative_path, title, platform, author)"""
    real = url
    m = re.search(r"v\.douyin\.com/([A-Za-z0-9_-]+)/", url)
    if not m and re.search(r"douyin\.com", url):
        mm = re.search(r"/(video|note)/(\d+)", url)
        if mm:
            real = f"https://www.iesdouyin.com/share/{mm.group(1)}/{mm.group(2)}/"
    if "iesdouyin" not in real:
        short = re.search(r"https://v\.douyin\.com/[A-Za-z0-9_-]+/?", url)
        if short:
            try:
                real = _OPENER.open(urllib.request.Request(short.group(0), headers={"User-Agent": UA_I}), timeout=15).geturl()
            except Exception:
                real = url
    base = os.path.join(MEDIA, "抖音", today())
    os.makedirs(base, exist_ok=True)
    old_untitled = os.path.join(base, "untitled")
    if os.path.isdir(old_untitled):
        try:
            shutil.rmtree(old_untitled)
        except Exception:
            pass
    pre_names = set(os.listdir(base))
    started = time.time()
    enc = urllib.parse.quote(real, safe="")
    api = f"{LITE_PARSER_URL}/video/share/url/parse?url={enc}"
    d = json.loads(fetch(api, timeout=40).decode("utf-8", "ignore"))
    if d.get("code") != 200:
        raise RuntimeError(f"轻解析失败: {d.get('msg','?')}")
    data = d.get("data") or {}
    author = (data.get("author") or {}).get("name") or "未知作者"
    title = clean_title(data.get("title")) or author

    def newest_mtime(p):
        try:
            return max((os.path.getmtime(os.path.join(p, f)) for f in os.listdir(p)), default=0)
        except OSError:
            return 0

    found = None
    waited = 0
    while waited < 90:
        now_names = set(os.listdir(base))
        new = now_names - pre_names
        stale = [n for n in (now_names & pre_names)
                 if os.path.isdir(os.path.join(base, n)) and newest_mtime(os.path.join(base, n)) >= started - 3]
        for name in list(new) + stale:
            p = os.path.join(base, name)
            if os.path.isdir(p):
                files = [f for f in os.listdir(p)]
                media_files = [f for f in files if f.lower().endswith((".mp4", ".jpg", ".jpeg", ".png", ".mov"))]
                if media_files:
                    found = p
                    break
        if found:
            break
        time.sleep(5)
        waited += 5
    if not found:
        if os.path.isdir(old_untitled):
            files = [f for f in os.listdir(old_untitled)]
            if any(f.lower().endswith((".mp4", ".mov", ".jpg", ".jpeg", ".png")) for f in files):
                found = old_untitled
    if not found:
        raise RuntimeError("等待抖音落盘超时")

    base_dir = sanitize_filename(title) or f"抖音_{int(time.time())}"
    target = os.path.join(base, base_dir)
    suffix = 2
    while os.path.isdir(target):
        target = os.path.join(base, f"{base_dir}_{suffix}")
        suffix += 1
    os.makedirs(target, exist_ok=True)
    final_dir = os.path.basename(target)
    moved_video = False
    for f in list(os.listdir(found)):
        if f.startswith("."):
            continue
        src = os.path.join(found, f)
        low = f.lower()
        if low.endswith((".mp4", ".mov", ".webm", ".mkv")):
            dst = os.path.join(target, f"{final_dir}.mp4")
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
            moved_video = True
        elif low.endswith((".jpg", ".jpeg", ".png")):
            nm = re.match(r"image_0*(\d+)\.(jpg|jpeg|png)", low)
            if nm:
                dst = os.path.join(target, f"{final_dir}_{nm.group(1)}.{nm.group(2)}")
            else:
                img_count = sum(1 for x in os.listdir(target) if x.lower().endswith((".jpg", ".jpeg", ".png")))
                dst = os.path.join(target, f"{final_dir}_{img_count+1}.{low.split('.')[-1]}")
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(src, dst)
        elif low.endswith(".mp3"):
            os.remove(src)
        else:
            shutil.move(src, os.path.join(target, f))
    if os.path.isdir(found) and os.listdir(found) == []:
        try:
            os.rmdir(found)
        except Exception:
            pass
    rel = os.path.join("抖音", today(), final_dir)
    d = os.path.join(MEDIA, rel)
    is_图文 = (not moved_video) and any(f.lower().endswith((".jpg", ".jpeg", ".png")) for f in os.listdir(d))
    gen_thumb(d, final_dir, is_video=not is_图文)
    return rel, title, "抖音", author


# ── 小红书 ─────────────────────────────────────────────
def xhs_expand(url):
    """展开 xhslink 短链"""
    r = _OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA_I}), timeout=15)
    real = r.geturl()
    m = re.search(r"/discovery/item/([a-f0-9]{24})", real)
    if not m:
        raise RuntimeError("小红书短链展开失败")
    nid = m.group(1)
    tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", real)
    if not tok:
        tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", url or "")
    tok = tok.group(1) if tok else None
    typ = "normal"
    tm = re.search(r"type=(\w+)", real)
    if tm:
        typ = tm.group(1)
    return real, nid, tok, typ


def xhs_get_page(real_url, nid, tok):
    """抓小红书页面"""
    url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
    if tok:
        url += f"?xsec_source=app_share&xsec_token={tok}="
    cookie = ""
    if XHS_COOKIE_FILE and os.path.isfile(XHS_COOKIE_FILE):
        cookie = open(XHS_COOKIE_FILE, encoding="utf-8").read().strip()
    passes = [(UA_D, ""), (UA_I, "")]
    if cookie:
        passes += [(UA_I, cookie), (UA_D, cookie)]
    for ua, ck in passes:
        try:
            headers = {"User-Agent": ua, "Referer": "https://www.xiaohongshu.com/"}
            if ck:
                headers["Cookie"] = ck
            req = urllib.request.Request(url, headers=headers)
            html = _OPENER.open(req, timeout=25).read().decode("utf-8", "ignore")
            if len(html) > 10000 and ("masterUrl" in html or "imageList" in html or ("nickname" in html and '"title"' in html)):
                return html, nid
        except Exception:
            pass
    raise RuntimeError("小红书页面抓取失败(可能是 xsec_token 过期,需在App重新分享该链接)")


def xhs_downloader_video(nid, real_url, title, d):
    """用 XHS-Downloader 下视频"""
    if not (XHS_COOKIE_FILE and os.path.isfile(XHS_COOKIE_FILE) and XHS_VENV and os.path.isfile(XHS_VENV)):
        return False
    tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", real_url or "")
    url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
    if tok:
        url += f"?xsec_source=app_share&xsec_token={tok.group(1)}="
    work = f"/tmp/xhs_vid_{nid}"
    shutil.rmtree(work, ignore_errors=True)
    try:
        cookie = open(XHS_COOKIE_FILE, encoding="utf-8").read().strip()
        subprocess.run([XHS_VENV, os.path.join(XHS_DIR, "main.py"), "--url", url,
                        "--cookie", cookie, "--work_path", work, "--download_record", "false"],
                       capture_output=True, timeout=240)
        mp4s = glob.glob(os.path.join(work, "Download", "**", "*.mp4"), recursive=True)
        if not mp4s:
            return False
        src = max(mp4s, key=os.path.getsize)
        if os.path.getsize(src) < 50000:
            return False
        shutil.copyfile(src, os.path.join(d, f"{title}.mp4"))
        shutil.rmtree(work, ignore_errors=True)
        return True
    except Exception:
        return False


def xhs_download_video(html, nid, title, author, real_url=""):
    """挖无水印流下载视频"""
    global SRC_TAG
    vurl, tag = pick_stream(html)
    SRC_TAG = tag or ""
    if not vurl:
        raise RuntimeError("无 masterUrl")
    d = os.path.join(MEDIA, "小红书", today(), title)
    os.makedirs(d, exist_ok=True)
    if tag == "259":
        if xhs_downloader_video(nid, real_url, title, d):
            gen_thumb(d, title, is_video=True)
            return os.path.join("小红书", today(), title)
    if tag == "259":
        WARN.append("平台只提供 259 水印流(无 309/258)，已入库但可能带水印")
    data = fetch(vurl, timeout=120, referer="https://www.xiaohongshu.com/")
    with open(os.path.join(d, f"{title}.mp4"), "wb") as f:
        f.write(data)
    gen_thumb(d, title, is_video=True)
    return os.path.join("小红书", today(), title)


def xhs_download_images(html, nid, title, author, real_url):
    """图文: 优先 XHS-Downloader 无水印原图; 失败降级 sns-webpic 直抓。
    Live 图: 解析页面 livePhoto 字段，配对下载 mp4（{title}_live{i}.mp4）"""
    global SRC_TAG
    SRC_TAG = "orig"
    d = os.path.join(MEDIA, "小红书", today(), title)
    os.makedirs(d, exist_ok=True)

    # ── 解析 Live 图视频 URL（小红书页面 imageList 里的 livePhoto 字段）──
    # 格式: "livePhotoUrl":"https://.../xxx.mp4" 或 "livePhoto":"..."
    live_urls = re.findall(r'"livePhoto(?:Url)?":"([^"]+\.mp4[^"]*)"', html)
    live_urls = [u.replace("\\u002F", "/").replace("\\/", "/") for u in live_urls]
    # 去重保序
    seen_live, live_picked = set(), []
    for u in live_urls:
        if u not in seen_live:
            seen_live.add(u)
            live_picked.append(u)

    if XHS_COOKIE_FILE and os.path.isfile(XHS_COOKIE_FILE) and XHS_VENV and os.path.isfile(XHS_VENV):
        tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", real_url or "")
        url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
        if tok:
            url += f"?xsec_source=app_share&xsec_token={tok.group(1)}="
        cookie = open(XHS_COOKIE_FILE, encoding="utf-8").read().strip()
        work = f"/tmp/xhs_worker_{nid}"
        shutil.rmtree(work, ignore_errors=True)
        try:
            subprocess.run(
                [XHS_VENV, os.path.join(XHS_DIR, "main.py"),
                 "--url", url, "--cookie", cookie, "--work_path", work,
                 "--image_format", "PNG", "--download_record", "false"],
                capture_output=True, timeout=150)
            pngs = glob.glob(os.path.join(work, "Download", "**", "*.png"), recursive=True)
            seen = set()
            uniq = []
            for x in pngs:
                key = os.path.basename(x)
                if key in seen:
                    continue
                seen.add(key)
                uniq.append(x)
            pngs = sorted(uniq)
            if pngs:
                for i, p in enumerate(pngs, 1):
                    shutil.copy(p, os.path.join(d, f"{title}_{i}.png"))
                # Live 图配对（XHS-Downloader 不下 live 视频，单独抓）
                _download_xhs_live_photos(live_picked, d, title)
                gen_thumb(d, title, is_video=False)
                return os.path.join("小红书", today(), title)
        except Exception:
            pass
    # 降级: sns-webpic 直抓(带水印)
    SRC_TAG = "render"
    urls = re.findall(r'https?://sns-webpic[^"\\s\\]|\\"\\'']+', html)
    urls = [u for u in urls if "h5_1080" in u or "!nd_prv" in u or "!nd_dft" in u]
    seen, picked = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            picked.append(u.replace("\\u002F", "/"))
    if not picked:
        picked = re.findall(r'https?://sns-webpic-qc\.xhscdn\.com[^"\\s\\]+?h5_1080[^"\\s]*', html)
        picked = [u.replace("\\u002F", "/") for u in picked]
        seen, dedup = set(), []
        for u in picked:
            if u not in seen:
                seen.add(u)
                dedup.append(u)
        picked = dedup
    ok = 0
    for i, u in enumerate(picked[:20], 1):
        try:
            data = fetch(u, timeout=60, referer="https://www.xiaohongshu.com/")
            open(os.path.join(d, f"{title}_{i}.jpg"), "wb").write(data)
            ok += 1
        except Exception:
            pass
    if ok == 0:
        raise RuntimeError("小红书图文下载失败(无水印+水印都拿不到)")
    # Live 图配对
    _download_xhs_live_photos(live_picked, d, title)
    gen_thumb(d, title, is_video=False)
    return os.path.join("小红书", today(), title)


def _download_xhs_live_photos(live_urls, d, title):
    """下载小红书 Live 图配对视频，转成 iPhone Live Photo 格式。
    - 下载 mp4 → 转 mov → 注入 Apple 元数据（ContentIdentifier + MediaGroupUUID）
    - 配对图片必须已存在（{title}_{i}.png/jpg）
    - 失败不阻塞图文入库（锦上添花）"""
    if not live_urls:
        return
    from utils import make_live_photo
    ok = 0
    converted = 0
    for i, u in enumerate(live_urls[:20], 1):
        # 视频先下到临时 mp4
        tmp_mp4 = os.path.join(d, f"{title}_live{i}.mp4")
        try:
            data = fetch(u, timeout=60, referer="https://www.xiaohongshu.com/")
            if len(data) < 1000:
                continue
            with open(tmp_mp4, "wb") as f:
                f.write(data)
            ok += 1
            # 找配对的图片（XHS-Downloader 存 png，sns-webpic 存 jpg）
            img = None
            for ext in (".png", ".jpg", ".jpeg"):
                cand = os.path.join(d, f"{title}_{i}{ext}")
                if os.path.isfile(cand):
                    img = cand
                    break
            if img:
                # 转成 iPhone Live Photo（mp4→mov + exiftool 注入元数据）
                make_live_photo(img, tmp_mp4)
                converted += 1
        except Exception:
            pass
    if ok > 0:
        msg = f"已下载 {ok} 个 Live 图"
        if converted > 0:
            msg += f"，{converted} 个已转 iPhone Live Photo 格式"
        WARN.append(msg)


def download_xiaohongshu(url):
    real, nid, tok, typ = xhs_expand(url)
    html, _ = xhs_get_page(real, nid, tok)
    meta = parse_xhs_meta(html)
    title = clean_title(meta["title"] or "小红书笔记")
    author = meta["author_name"]
    avatar = meta["author_avatar"]
    has_video = meta["has_video"]
    if typ == "video" or has_video or ('"masterUrl"' in html and '"imageList"' not in html):
        rel = xhs_download_video(html, nid, title, author, real_url=real)
        return rel, title, "小红书", avatar
    else:
        rel = xhs_download_images(html, nid, title, author, real)
        return rel, title, "小红书", avatar


# ── 微博 ──────────────────────────────────────────────
def _wb_pid_from_url(u):
    m = re.search(r"/(?:mw2000|large|orj\d+|original)/([a-zA-Z0-9]+)\.", u)
    return m.group(1) if m else None


def download_weibo(url):
    """微博链接下载"""
    real = url
    if "t.cn" in url or "/short/" in url:
        try:
            real = _OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA_I}), timeout=15).geturl()
        except Exception:
            real = url
    enc = urllib.parse.quote(real, safe="")
    api = f"{LITE_PARSER_URL}/video/share/url/parse?url={enc}"
    last_err = None
    d = None
    for attempt in range(2):
        try:
            d = json.loads(fetch(api, timeout=60).decode("utf-8", "ignore"))
            break
        except Exception as e:
            last_err = e
            time.sleep(3)
    if d is None:
        raise RuntimeError(f"轻解析微博接口两次失败: {last_err}")
    if d.get("code") != 200:
        raise RuntimeError(f"轻解析微博失败: {d.get('msg','?')}")
    data = d.get("data") or {}
    raw_title = (data.get("title") or "").strip()
    title = clean_title(raw_title) or "微博"
    author = (data.get("author") or {}).get("name") or "微博用户"
    video_url = data.get("video_url") or ""
    images = data.get("images") or []

    if not video_url and not images:
        raise RuntimeError("微博无媒体(图/视频都没有)")

    base = os.path.join(MEDIA, "微博", today(), title)
    os.makedirs(base, exist_ok=True)

    saved_video = False
    if video_url:
        try:
            r = _OPENER.open(urllib.request.Request(video_url, headers={
                "User-Agent": UA_I, "Referer": "https://weibo.com/",
            }), timeout=120)
            with open(os.path.join(base, f"{title}.mp4"), "wb") as f:
                f.write(r.read())
            saved_video = True
        except Exception as e:
            WARN.append(f"微博视频下载失败: {str(e)[:80]}")

    saved_n = 0
    if not saved_video and images:
        for idx, im in enumerate(images, 1):
            short_url = im.get("url") or ""
            live_url = im.get("live_photo_url") or ""
            pid = _wb_pid_from_url(short_url)
            orig_url = f"https://wx1.sinaimg.cn/original/{pid}.jpg" if pid else short_url
            try:
                r = _OPENER.open(urllib.request.Request(orig_url, headers={
                    "User-Agent": UA_I, "Referer": "https://weibo.com/",
                }), timeout=60)
                with open(os.path.join(base, f"{title}_{idx}.jpg"), "wb") as f:
                    f.write(r.read())
                saved_n += 1
            except Exception:
                if short_url:
                    try:
                        r = _OPENER.open(urllib.request.Request(short_url, headers={
                            "User-Agent": UA_I, "Referer": "https://weibo.com/",
                        }), timeout=60)
                        with open(os.path.join(base, f"{title}_{idx}.jpg"), "wb") as f:
                            f.write(r.read())
                        saved_n += 1
                    except Exception:
                        pass
            if live_url:
                try:
                    rr = _OPENER.open(urllib.request.Request(live_url, headers={
                        "User-Agent": UA_I, "Referer": "https://weibo.com/",
                    }), timeout=60)
                    tmp_mov = os.path.join(base, f"{title}_{idx}_live.mov")
                    with open(tmp_mov, "wb") as f:
                        f.write(rr.read())
                    img_path = os.path.join(base, f"{title}_{idx}.jpg")
                    if os.path.isfile(img_path):
                        from utils import make_live_photo
                        make_live_photo(img_path, tmp_mov)
                except Exception:
                    pass

    if not saved_video and saved_n == 0:
        raise RuntimeError("微博媒体下载全部失败(原图+压缩图都拿不到)")

    rel = os.path.join("微博", today(), title)

    # 清理 lite.parser 残留孤儿目录（必须在 rel 赋值之后）
    try:
        import pwd as _pwd
        date_dir = os.path.dirname(rel)
        full_date = os.path.join(MEDIA, date_dir)
        if os.path.isdir(full_date):
            from db import get_paths_in_dir
            refs = get_paths_in_dir(date_dir)
            for sibling in os.listdir(full_date):
                if sibling == title or sibling == os.path.basename(rel):
                    continue
                full = os.path.join(full_date, sibling)
                if not os.path.isdir(full):
                    continue
                try:
                    own = _pwd.getpwuid(os.stat(full).st_uid).pw_name
                except Exception:
                    continue
                if own != "lite.parser" or "#" not in sibling:
                    continue
                sib_rel = os.path.join(date_dir, sibling)
                if sib_rel in refs:
                    continue
                try:
                    shutil.rmtree(full)
                except Exception:
                    pass
    except Exception:
        pass

    gen_thumb(base, title, is_video=saved_video)
    return rel, title, "微博", author


# ── X / Twitter ──────────────────────────────────────────────
def download_x(url):
    """X/Twitter 链接下载"""
    url = re.sub(r'/video/\d+', '', url)
    m = re.search(r'/([^/]+)/status/(\d+)', url)
    if not m:
        raise RuntimeError("X 链接格式错(需 /<user>/status/<id>)")
    user, tid = m.group(1), m.group(2)
    api = f"https://api.fxtwitter.com/{user}/status/{tid}"
    with _OPENER.open(api, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    tweet = d.get("tweet") or {}
    text = (tweet.get("text") or "").strip()
    author = (tweet.get("author") or {}).get("name") or "未知作者"
    media_all = (tweet.get("media") or {}).get("all") or []
    if not media_all:
        raise RuntimeError("X 推文无媒体")

    if text:
        title = clean_title(text[:60]) or f"{author}_{tid[-8:]}"
    else:
        title = f"{author}_{tid[-8:]}"

    videos = [m_ for m_ in media_all if m_.get("type") in ("video", "gif")]
    if videos:
        variants = videos[0].get("variants") or []
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
        if mp4s:
            best = max(mp4s, key=lambda v: v.get("bitrate", 0))
            vurl = best["url"]
            ext = "mp4"
        else:
            vurl = videos[0].get("url")
            ext = "mp4"
    else:
        vurl = media_all[0].get("url", "")
        ext = "jpg"

    if not vurl:
        raise RuntimeError("X 媒体 url 为空")

    d = os.path.join(MEDIA, "X", today(), title)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{title}_1.{ext}")

    proxy_handler = urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
    opener = urllib.request.build_opener(proxy_handler)
    opener.addheaders = [("User-Agent", UA_D)]
    try:
        with opener.open(vurl, timeout=120) as r:
            data = r.read()
    except Exception:
        with urllib.request.urlopen(urllib.request.Request(vurl, headers={"User-Agent": UA_D}), timeout=120) as r:
            data = r.read()
    with open(path, "wb") as f:
        f.write(data)

    gen_thumb(d, title, is_video=(ext == "mp4"))
    return os.path.join("X", today(), title), title, "X", author


# ── TikTok (tikwm API) ──────────────────────────────────────
def download_tiktok(url):
    """TikTok 链接: tikwm API 解析 → 无水印视频/图片下载 → 归档。
    tikwm 返回 data.play(无水印mp4) / data.wmplay(带水印) / data.images(图文)。
    海外 CDN 需走代理。"""
    # 提取视频 ID（用于缓存和校验）
    m = re.search(r"/video/(\d+)", url) or re.search(r"/@[\w.]+/video/(\d+)", url)
    if not m:
        # 短链 t.tiktok.com 需展开
        if "t.tiktok.com" in url or "vm.tiktok.com" in url:
            try:
                real = _OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA_I}), timeout=15).geturl()
                m = re.search(r"/video/(\d+)", real)
                if m:
                    url = real
            except Exception:
                pass
    api = f"https://www.tikwm.com/api/?url={urllib.parse.quote(url, safe='')}"
    resp = fetch(api, timeout=30)
    d = json.loads(resp.decode("utf-8", "ignore"))
    if d.get("code") != 0:
        raise RuntimeError(f"tikwm 解析失败: {d.get('msg', '?')}")
    data = d.get("data") or {}
    author_obj = data.get("author") or {}
    author = author_obj.get("nickname") or author_obj.get("unique_id") or "TikTok用户"
    avatar = author_obj.get("avatar") or ""
    text = (data.get("title") or "").strip()
    title = clean_title(text[:60]) or f"TikTok_{author[:20]}"

    # 媒体选择：图文(images)优先，否则视频(play)
    images = data.get("images") or []
    play_url = data.get("play") or data.get("wmplay") or ""

    d = os.path.join(MEDIA, "TikTok", today(), title)
    os.makedirs(d, exist_ok=True)

    saved = 0
    if images:
        # 图文：逐张下载
        for i, img_url in enumerate(images, 1):
            try:
                data_bytes = fetch(img_url, timeout=60, use_proxy=True)
                ext = "jpg"
                if ".png" in img_url:
                    ext = "png"
                elif ".webp" in img_url:
                    ext = "webp"
                with open(os.path.join(d, f"{title}_{i}.{ext}"), "wb") as f:
                    f.write(data_bytes)
                saved += 1
            except Exception as e:
                log.warning(f"TikTok 图 {i} 下载失败: {e}")
        is_video = False
    elif play_url:
        # 视频：下无水印 mp4
        try:
            data_bytes = fetch(play_url, timeout=120, use_proxy=True)
            with open(os.path.join(d, f"{title}.mp4"), "wb") as f:
                f.write(data_bytes)
            saved = 1
        except Exception as e:
            # 无水印失败，试带水印
            wm = data.get("wmplay") or ""
            if wm:
                try:
                    data_bytes = fetch(wm, timeout=120, use_proxy=True)
                    with open(os.path.join(d, f"{title}.mp4"), "wb") as f:
                        f.write(data_bytes)
                    saved = 1
                    global SRC_TAG
                    SRC_TAG = "wm"
                    WARN.append("TikTok 无水印下载失败，已用带水印版")
                except Exception:
                    pass
            if saved == 0:
                raise RuntimeError(f"TikTok 视频下载失败: {e}")
        is_video = True
    else:
        raise RuntimeError("TikTok 无媒体(图/视频都没有)")

    gen_thumb(d, title, is_video=is_video)
    return os.path.join("TikTok", today(), title), title, "TikTok", author


# ════════════════════════════════════════════════════════════
# 主页批量解析（profile → 单条 URL 列表）
# ════════════════════════════════════════════════════════════

PROFILE_MAX = 20  # 主页最多展开条数


def parse_tiktok_profile(url: str) -> List[str]:
    """TikTok 主页 → 最新 N 条视频 URL。
    tikwm API: /api/user/posts?username=xxx&count=20"""
    m = re.search(r"tiktok\.com/@([\w.]+)", url)
    if not m:
        raise RuntimeError("TikTok 主页格式错（需 /@username）")
    username = m.group(1)
    api = f"https://www.tikwm.com/api/user/posts?username={urllib.parse.quote(username)}&count={PROFILE_MAX}"
    resp = fetch(api, timeout=30)
    d = json.loads(resp.decode("utf-8", "ignore"))
    if d.get("code") != 0:
        raise RuntimeError(f"tikwm 主页解析失败: {d.get('msg', '?')}")
    videos = (d.get("data") or {}).get("videos") or []
    urls = []
    for v in videos:
        vid = v.get("video_id") or v.get("id")
        if vid:
            urls.append(f"https://www.tiktok.com/@{username}/video/{vid}")
    return urls[:PROFILE_MAX]


def parse_weibo_profile(url: str) -> List[str]:
    """微博主页 → 最新 N 条微博 URL。
    m.weibo.cn API: container/getIndex?type=uid&value=<uid>&containerid=107603<uid>"""
    # 提取 UID
    m = re.search(r"weibo\.com/u/(\d+)", url) or re.search(r"m\.weibo\.cn/profile/(\d+)", url)
    if not m:
        # 用户名形式 → 需要展开拿 uid
        try:
            real = _OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA_I}), timeout=15).geturl()
            m = re.search(r"weibo\.com/u/(\d+)", real) or re.search(r"m\.weibo\.cn/profile/(\d+)", real)
        except Exception:
            pass
    if not m:
        raise RuntimeError("微博主页无法提取 UID")
    uid = m.group(1)
    # 主页时间线
    api = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}&containerid=107603{uid}"
    headers = {"User-Agent": UA_I, "Referer": f"https://m.weibo.cn/profile/{uid}",
               "X-Requested-With": "XMLHttpRequest"}
    req = urllib.request.Request(api, headers=headers)
    resp = _OPENER.open(req, timeout=20).read().decode("utf-8", "ignore")
    d = json.loads(resp)
    cards = d.get("data", {}).get("cards", [])
    urls = []
    for card in cards:
        mblog = card.get("mblog")
        if not mblog:
            continue
        bid = mblog.get("bid")
        if bid:
            urls.append(f"https://m.weibo.cn/detail/{bid}")
    return urls[:PROFILE_MAX]


def parse_xhs_profile(url: str) -> List[str]:
    """小红书主页 → 最新 N 条笔记 URL。
    需要 cookie。解析主页 HTML 拿 noteId 列表，再拼成单条 URL。"""
    # 提取用户 ID
    m = re.search(r"user/profile/([a-f0-9]+)", url)
    if not m:
        raise RuntimeError("小红书主页格式错（需 /user/profile/<id>）")
    user_id = m.group(1)
    cookie = ""
    if XHS_COOKIE_FILE and os.path.isfile(XHS_COOKIE_FILE):
        cookie = open(XHS_COOKIE_FILE, encoding="utf-8").read().strip()
    if not cookie:
        raise RuntimeError("小红书主页解析需要 cookie（XHS_COOKIE_FILE 未配置）")
    # 抓主页 HTML
    page_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    headers = {"User-Agent": UA_D, "Referer": "https://www.xiaohongshu.com/", "Cookie": cookie}
    req = urllib.request.Request(page_url, headers=headers)
    html = _OPENER.open(req, timeout=25).read().decode("utf-8", "ignore")
    # 从 HTML 里挖 noteId（页面 JSON 嵌在 window.__INITIAL_STATE__ 里）
    note_ids = re.findall(r'"noteId":"([a-f0-9]+)"', html)
    # 去重保序
    seen, uniq = set(), []
    for nid in note_ids:
        if nid not in seen:
            seen.add(nid)
            uniq.append(nid)
    urls = [f"https://www.xiaohongshu.com/discovery/item/{nid}" for nid in uniq]
    return urls[:PROFILE_MAX]


def parse_douyin_profile(url: str) -> List[str]:
    """抖音主页 → 最新 N 条视频 URL。
    抖音 web 端需 _signature，反爬最严。优先走轻解析服务（如果支持）。
    目前为实验性实现，可能失败。"""
    sec_uid = ""
    m = re.search(r"douyin\.com/user/([^/?]+)", url)
    if m:
        sec_uid = m.group(1)
    if not sec_uid:
        raise RuntimeError("抖音主页无法提取 sec_uid")
    # 尝试轻解析服务的主页接口（如果轻解析服务支持）
    api = f"{LITE_PARSER_URL}/user/videos?sec_uid={urllib.parse.quote(sec_uid)}&count={PROFILE_MAX}"
    try:
        resp = fetch(api, timeout=40)
        d = json.loads(resp.decode("utf-8", "ignore"))
        if d.get("code") == 200:
            videos = d.get("data", {}).get("videos") or []
            urls = []
            for v in videos:
                vid = v.get("video_id") or v.get("aweme_id")
                if vid:
                    urls.append(f"https://www.douyin.com/video/{vid}")
            if urls:
                return urls[:PROFILE_MAX]
    except Exception:
        pass
    raise RuntimeError("抖音主页解析失败（轻解析服务可能不支持主页接口，或需 _signature）")


def parse_x_profile(url: str) -> List[str]:
    """X/Twitter 主页 → 最新 N 条推文 URL。
    fxtwitter 不支持主页。用 nitter 实例或 RSS 兜底。"""
    m = re.search(r"x\.com/([\w]+)/?$", url) or re.search(r"twitter\.com/([\w]+)/?$", url)
    if not m:
        raise RuntimeError("X 主页格式错（需 /username）")
    username = m.group(1)
    # 尝试 fxtwitter 的用户 timeline（实验性）
    api = f"https://api.fxtwitter.com/{username}"
    try:
        resp = _OPENER.open(urllib.request.Request(api, headers={"User-Agent": UA_D}), timeout=20)
        d = json.loads(resp.read().decode("utf-8", "ignore"))
        # fxtwitter 用户 API 返回最近推文（如果有）
        tweets = d.get("tweets") or []
        urls = [f"https://x.com/{username}/status/{t.get('id')}" for t in tweets if t.get("id")]
        if urls:
            return urls[:PROFILE_MAX]
    except Exception:
        pass
    raise RuntimeError("X 主页解析失败（fxtwitter 不支持用户 timeline，建议用单条链接）")


# 主页解析分发
PROFILE_PARSERS = {
    "tiktok": parse_tiktok_profile,
    "weibo": parse_weibo_profile,
    "xiaohongshu": parse_xhs_profile,
    "douyin": parse_douyin_profile,
    "x": parse_x_profile,
}


def detect_profile_platform(url: str) -> str:
    """判断主页 URL 属于哪个平台"""
    if "tiktok.com/@" in url:
        return "tiktok"
    if "weibo.com" in url or "m.weibo.cn/profile" in url:
        return "weibo"
    if "xiaohongshu.com/user/profile" in url or "xhslink.com/user" in url:
        return "xiaohongshu"
    if "douyin.com/user/" in url:
        return "douyin"
    if "x.com/" in url or "twitter.com/" in url:
        return "x"
    return ""


def process_profile(item):
    """处理主页任务：解析主页 → 拿单条 URL 列表 → 去重 → 加入队列"""
    url = item["url"].strip()
    platform = detect_profile_platform(url)
    if not platform:
        item["status"] = "failed"
        item["message"] = "无法识别主页平台"
        return
    parser = PROFILE_PARSERS.get(platform)
    if not parser:
        item["status"] = "failed"
        item["message"] = f"不支持 {platform} 主页解析"
        return
    try:
        from queue_manager import add_sub_tasks
        from db import get_item_by_url
        urls = parser(url)
        # 去掉 DB 里已收藏的
        filtered = []
        for u in urls:
            if not get_item_by_url(u):
                filtered.append(u)
        added = add_sub_tasks(item["id"], filtered, platform)
        item["status"] = "done"
        item["title"] = f"主页展开 {platform}"
        item["message"] = f"解析 {len(urls)} 条，去重后 {len(filtered)} 条已加入队列，新增 {added} 条"
        log.info(f"主页展开 [{platform}] {url}: {len(urls)} 条 → 加入 {added} 条")
    except Exception as e:
        item["status"] = "failed"
        item["message"] = f"主页解析失败: {str(e)[:120]}"
        log.error(f"主页解析失败 [{platform}] {url}: {e}")


def ingest(rel, title, url, platform, author=None, avatar=None):
    """调用 ingest.py 入库"""
    args = [PY, INGEST, f"--platform={platform}", f"--url={url}", f"--path={rel}", f"--title={title}"]
    if author and author != "未知作者":
        args.append(f"--author={author}")
    if avatar:
        args.append(f"--avatar={avatar}")
    r = subprocess.run(args, capture_output=True, timeout=60)
    out = (r.stdout or b"").decode("utf-8", "ignore")
    err = (r.stderr or b"").decode("utf-8", "ignore")
    if "已收藏过" in out or "跳过" in out:
        d = os.path.join(MEDIA, rel)
        referenced = False
        try:
            from db import is_path_referenced
            referenced = is_path_referenced(rel)
        except Exception:
            referenced = True
        if os.path.isdir(d) and not referenced:
            shutil.rmtree(d, ignore_errors=True)
        return False
    if r.returncode != 0:
        raise RuntimeError(f"入库失败({r.returncode}): {(err or out).strip()[:150]}")
    return True


SCAN_EVERY = int(os.environ.get("WM_SCAN_EVERY", "20"))
WM_SCAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wm_scan.py")


def bump_scan_counter():
    """每成功入库 SCAN_EVERY 条，后台触发一次水印自检"""
    try:
        f = os.path.join(MEDIA, ".wm_counter")
        n = int(open(f).read().strip() or 0) if os.path.isfile(f) else 0
        n += 1
        open(f, "w").write(str(n))
        if n >= SCAN_EVERY:
            open(f, "w").write("0")
            py = sys.executable   # 宿主 python3（numpy/PIL 都在）
            subprocess.Popen([py, WM_SCAN, "--limit", str(SCAN_EVERY)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            log.info(f"水印自检已触发（累计 {SCAN_EVERY} 条）")
    except Exception as e:
        log.warning(f"水印自检触发失败: {e}")


def process_one(item):
    global SRC_TAG
    SRC_TAG = ""
    url = item["url"].strip()
    if not url.startswith("http"):
        item["status"] = "failed"
        item["message"] = "无效链接"
        return
    # 主页任务走单独流程
    if item.get("type") == "profile":
        process_profile(item)
        return
    try:
        author = None
        avatar = None
        if "douyin" in url or "iesdouyin" in url:
            rel, title, plat, author = download_douyin(url)
        elif "xhslink" in url or "xiaohongshu" in url:
            rel, title, plat, avatar = download_xiaohongshu(url)
        elif "x.com" in url or "twitter.com" in url:
            rel, title, plat, author = download_x(url)
        elif "weibo.com" in url or "m.weibo.cn" in url or "t.cn" in url:
            rel, title, plat, author = download_weibo(url)
        elif "tiktok.com" in url:
            rel, title, plat, author = download_tiktok(url)
        else:
            item["status"] = "failed"
            item["message"] = "不支持的平台(支持抖音/小红书/X/微博/TikTok)"
            return
        ingest(rel, title, item.get("original_url") or url, plat,
               author=author or None, avatar=avatar if plat == "小红书" else None,
               src_tag=SRC_TAG)
        bump_scan_counter()
        item["status"] = "done"
        item["title"] = title
        item["path"] = rel
        # 重置重试计数（成功后清零）
        item["retries"] = 0
        if WARN:
            item["message"] = "⚠️ " + "; ".join(WARN)
            WARN.clear()
        else:
            item["message"] = "完成"
    except Exception as e:
        retries = item.get("retries", 0)
        # 自动重试：最多 2 次，间隔递增（10s, 30s 由下次 cron 触发）
        if retries < 2:
            item["retries"] = retries + 1
            item["status"] = "pending"  # 改回 pending，下次 drain 再试
            item["message"] = f"自动重试中({item['retries']}/2): {str(e)[:80]}"
            log.warning(f"自动重试 [{url}] ({item['retries']}/2): {e}")
        else:
            item["status"] = "failed"
            item["message"] = str(e)[:150]
            log.error(f"处理失败 [{url}] (已重试{retries}次): {e}")
            # 失败 Bark 通知
            _notify_failure(url, str(e)[:100])


def _notify_failure(url: str, reason: str):
    """失败超过重试次数后推送 Bark 通知"""
    if not BARK_URL:
        return
    try:
        import urllib.parse as up
        title = "❌ 拾光集下载失败"
        body = f"链接: {url[:60]}\n原因: {reason}"
        u = f"{BARK_URL.rstrip('/')}/{up.quote(title)}/{up.quote(body)}"
        _OPENER.open(urllib.request.Request(u), timeout=8).read()
    except Exception:
        pass


def drain():
    q = load_queue()
    if not q:
        return
    # 清理过期条目
    from queue_manager import cleanup_queue
    cleanup_queue()
    q = load_queue()
    if not q:
        return

    changed = False
    for item in q:
        if item.get("status") == "pending":
            item["status"] = "processing"
            changed = True
            break
    if changed:
        save_queue(q)
    for item in q:
        if item.get("status") == "processing":
            process_one(item)
            # done 的立即移除；failed/pending(重试中) 保留
            if item.get("status") == "done":
                q = [it for it in q if it.get("id") != item.get("id")]
            save_queue(q)
            break


def main():
    if os.path.isfile(LOCK):
        try:
            age = time.time() - os.path.getmtime(LOCK)
            if age < 90:
                return
        except Exception:
            pass
    open(LOCK, "w").close()
    try:
        drain()
    finally:
        try:
            os.remove(LOCK)
        except Exception:
            pass


if __name__ == "__main__":
    main()
