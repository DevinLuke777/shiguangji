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
import contextlib
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import time
import urllib.error
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
from utils import (clean_title, sanitize_filename, gen_thumb_ffmpeg, pick_stream,
                  parse_xhs_meta, trash_duplicate)
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
        cands = sorted(glob.glob(os.path.join(d, "*.png")) + glob.glob(os.path.join(d, "*.jpg"))
                       + glob.glob(os.path.join(d, "*.jpeg")) + glob.glob(os.path.join(d, "*.webp")))
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
    title = clean_title(data.get("title")) or clean_title(author) or f"抖音_{int(time.time())}"

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
    """展开 xhslink 短链；也接受已经是 /explore/<id> 或 /discovery/item/<id> 的规范链接"""
    m = re.search(r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-fA-F]{24})", url or "")
    if m:
        tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+=?)", url or "")
        tm = re.search(r"type=(\w+)", url or "")
        return url, m.group(1), tok.group(1) if tok else None, tm.group(1) if tm else "normal"
    r = _OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA_I}), timeout=15)
    real = r.geturl()
    m = re.search(r"/discovery/item/([0-9a-fA-F]{24})", real) or \
        re.search(r"/explore/([0-9a-fA-F]{24})", real)
    if not m:
        # 小红书风控常把短链跳到 /404/sec_xxx?originalUrl=<编码后的真链>，要解开再匹配
        om = re.search(r"[?&]originalUrl=([^&]+)", real or "")
        if om:
            try:
                real2 = urllib.parse.unquote(om.group(1))
                m = re.search(r"/(?:discovery/item|explore)/([0-9a-fA-F]{24})", real2)
                if m:
                    real = real2
            except Exception:
                pass
    if not m:
        raise RuntimeError(f"小红书短链展开失败（落到: {real[:80]}）")
    nid = m.group(1)
    tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+=?)", real)
    if not tok:
        tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+=?)", url or "")
    tok = tok.group(1) if tok else None
    typ = "normal"
    tm = re.search(r"type=(\w+)", real)
    if tm:
        typ = tm.group(1)
    return real, nid, tok, typ


def _xhs_cookie():
    """读 cookie 文件（可能不存在）"""
    if XHS_COOKIE_FILE and os.path.isfile(XHS_COOKIE_FILE):
        try:
            return open(XHS_COOKIE_FILE, encoding="utf-8").read().strip()
        except Exception:
            return ""
    return ""


def xhs_has_note(html):
    """页面里是否已经有笔记数据（视频流 / 图集 / noteDetailMap 任一）"""
    return bool(html) and ("masterUrl" in html or "imageList" in html)


def _unesc(u):
    return (u or "").replace("\\u002F", "/").replace("\\/", "/")


def xhs_note_state(html):
    """解析 window.__INITIAL_STATE__ → noteDetailMap 里的 note dict；失败返回 {}。
    比正则挖字段可靠：能一次拿到 video.media.stream(多编码) + imageList(含 livePhoto)。"""
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html or "")
    if not m:
        return {}
    start = m.end()
    depth, end = 0, None
    for i in range(start, len(html)):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if not end:
        return {}
    try:
        st = json.loads(html[start:end].replace("undefined", "null"))
    except Exception:
        return {}
    return _xhs_pick_note(st)


def _xhs_pick_note(st):
    """在小红书的几种 state 形状里找 note 对象：
    1) 前端路由渲染：state.note.noteDetailMap[<id>].note
    2) 服务端直出：state.noteData.data.noteData（本身就是 note）
    3) 兜底：任意层级的 noteDetailMap"""
    if not isinstance(st, dict):
        return {}
    ndm = ((st.get("note") or {}).get("noteDetailMap") or {})
    if isinstance(ndm, dict):
        for v in ndm.values():
            n = (v or {}).get("note") or {}
            if n and n.get("imageList"):
                return n
    nd = st.get("noteData") or {}
    for cand in ((nd.get("data") or {}).get("noteData"), nd.get("note")):
        if isinstance(cand, dict) and (cand.get("imageList") or cand.get("video")):
            return cand
    # 深搜兜底
    def _walk(o, depth=0):
        if depth > 6:
            return None
        if isinstance(o, dict):
            if "noteDetailMap" in o and isinstance(o["noteDetailMap"], dict):
                for v in o["noteDetailMap"].values():
                    n = (v or {}).get("note") or {}
                    if n:
                        return n
            for v in o.values():
                r = _walk(v, depth + 1)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = _walk(v, depth + 1)
                if r:
                    return r
        return None
    return _walk(st) or {}


def xhs_page_via_browser(nid, tok):
    """真实浏览器渲染笔记页。
    小红书对「仅 App 内查看」的笔记和风控命中的请求只下发壳页
    （纯 HTTP 只能看到「当前内容仅支持在小红书 APP 内查看」），
    浏览器执行完前端 JS 后 __INITIAL_STATE__ / DOM 里才有 masterUrl、imageList。"""
    cookie = _xhs_cookie()
    urls = []
    if tok:
        urls.append(f"https://www.xiaohongshu.com/explore/{nid}?xsec_source=pc_feed&xsec_token={tok}")
    urls.append(f"https://www.xiaohongshu.com/explore/{nid}")
    if tok:
        urls.append(f"https://www.xiaohongshu.com/discovery/item/{nid}?xsec_source=app_share&xsec_token={tok}")
    reason = "未尝试"
    feed_msg = ""
    with browser_page() as page:
        if cookie:
            try:
                page.context.set_extra_http_headers({"Cookie": cookie})
            except Exception:
                pass

        def _on_resp(r):
            nonlocal feed_msg
            if "/api/sns/web/v1/feed" in r.url and not feed_msg:
                try:
                    if r.status >= 400 or '"success":false' in r.text()[:200]:
                        feed_msg = r.text()[:120].strip()
                except Exception:
                    pass

        try:
            page.on("response", _on_resp)
        except Exception:
            pass
        for u in urls:
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                reason = f"打开页面失败({type(e).__name__})"
                continue
            for _ in range(15):
                page.wait_for_timeout(1000)
                try:
                    html = page.content()
                except Exception:
                    html = ""
                if xhs_has_note(html):
                    return html
                try:
                    cur = page.url or ""
                except Exception:
                    cur = ""
                # 已经掉到 404 / 首页说明这条笔记的 token 无效，没必要再等
                if "/404" in cur or cur.rstrip("/").endswith("/explore") or "/login" in cur:
                    break
            try:
                reason = f"渲染后仍无笔记数据(落在 {page.url[:70]})"
            except Exception:
                reason = "渲染后仍无笔记数据"
    if feed_msg:
        reason += f"；feed 接口返回 {feed_msg}"
    raise RuntimeError(reason)


def xhs_get_page(real_url, nid, tok):
    """抓小红书笔记页：先 HTTP（快），拿不到笔记数据再用浏览器渲染兜底。
    返回 (html, nid)。全失败时错误信息写清每一步卡在哪。"""
    url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
    if tok:
        tok2 = tok if tok.endswith("=") else tok + "="   # 官方 token 以 = 收尾，别重复加
        url += f"?xsec_source=app_share&xsec_token={tok2}"
    cookie = _xhs_cookie()
    passes = [(UA_D, ""), (UA_I, "")]
    if cookie:
        passes += [(UA_I, cookie), (UA_D, cookie)]
    why = []
    app_only = False
    for ua, ck in passes:
        try:
            headers = {"User-Agent": ua, "Referer": "https://www.xiaohongshu.com/"}
            if ck:
                headers["Cookie"] = ck
            req = urllib.request.Request(url, headers=headers)
            html = _OPENER.open(req, timeout=25).read().decode("utf-8", "ignore")
            if len(html) > 10000 and xhs_has_note(html):
                return html, nid
            if "仅支持在小红书 APP 内查看" in html or "IP at risk" in html:
                app_only = True
                break          # 换 UA 也没用，直接上浏览器
            why.append(f"HTTP {len(html)}B 无笔记数据")
        except Exception as e:
            why.append(f"HTTP {type(e).__name__}")
    if app_only:
        why.insert(0, "HTTP 只拿到「仅支持在小红书 APP 内查看」/风控壳页")
    try:
        html = xhs_page_via_browser(nid, tok)
        log.info("小红书页面 HTTP 无数据，浏览器渲染成功")
        return html, nid
    except Exception as e:
        why.append(f"浏览器渲染: {str(e)[:80]}")
    raise RuntimeError("小红书笔记数据抓取失败：" + "；".join(dict.fromkeys(why))
                       + "（token 可能已过期，需在 App 重新分享该笔记）")


def xhs_stream_urls(html, nid=""):
    """视频流候选，按画质/水印优先级排：309(h265 无水印) → 258(h264 无水印)
    → 未标注(按 h264 当无水印) → 259(带水印)。
    只认真·视频流：图文/实况笔记里带 livePhoto 的 mp4 是 Live 片段（streamType 19），要排除。"""
    note = xhs_note_state(html)
    ntype = (note.get("type") or "").lower()
    if ntype and ntype != "video":
        return []          # 图文/实况笔记，走图片+实况路径
    cands = []
    stream = (((note.get("video") or {}).get("media") or {}).get("stream") or {})
    for codec, arr in (stream or {}).items():
        if not isinstance(arr, list):
            continue
        for it in arr:
            u = _unesc((it or {}).get("masterUrl") or (it or {}).get("url") or "")
            if not u:
                continue
            tag = "309" if codec in ("h265", "h266", "av1") else ("258" if codec == "h264" else "")
            if "_259.mp4" in u:
                tag = "259"
            cands.append((u, tag))
    if not cands:
        for m in re.finditer(r'"masterUrl":"([^"]+)"', html or ""):
            # 前 600 字符里带 WEB_LIVEPHOTO 的是实况片段，不是视频流
            ctx = (html or "")[max(0, m.start() - 600):m.start()]
            if "WEB_LIVEPHOTO" in ctx:
                continue
            cands.append((_unesc(m.group(1)), ""))
    seen, out = set(), []
    for u, tag in cands:
        if u.startswith("http://"):
            u = "https://" + u[len("http://"):]
        if u in seen:
            continue
        seen.add(u)
        if not tag:
            mt = re.search(r"_(259|258|309)\.mp4", u)
            tag = mt.group(1) if mt else "258"
        out.append((u, tag))
    rank = {"309": 0, "258": 1, "": 2, "259": 3}
    out.sort(key=lambda x: rank.get(x[1], 2))
    return out



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
    """下视频：先按优先级试页面 JSON/签名接口给出的真实流，逐个候选；
    全部失败再用 XHS-Downloader（签名接口实现）兜底。"""
    global SRC_TAG
    d = os.path.join(MEDIA, "小红书", today(), title)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"{title}.mp4")
    cands = xhs_stream_urls(html, nid)
    errs = []
    if not cands:
        errs.append("页面/浏览器 HTML 与 __INITIAL_STATE__ 里都没有 masterUrl")
    for vurl, tag in cands:
        try:
            data = fetch(vurl, timeout=180, referer="https://www.xiaohongshu.com/")
        except Exception as e:
            errs.append(f"流{tag}下载失败({type(e).__name__})")
            continue
        if len(data) < 200 * 1024:
            errs.append(f"流{tag}只有 {len(data)}B")
            continue
        with open(dst, "wb") as f:
            f.write(data)
        SRC_TAG = tag
        if tag == "259":
            WARN.append("平台只提供 259 水印流(无 309/258)，已入库但可能带水印")
        gen_thumb(d, title, is_video=True)
        return _xhs_rel(d)
    # 兜底 1: XHS-Downloader（venv，签名接口）
    if xhs_downloader_video(nid, real_url, title, d):
        SRC_TAG = "259"
        gen_thumb(d, title, is_video=True)
        return _xhs_rel(d)
    errs.append("XHS-Downloader 兜底也没下到 mp4")
    # 兜底 2: xhs_api.py（venv，签名接口 + 浏览器/接口混用）
    if xhs_api_download(nid, real_url, title, d, video=True):
        gen_thumb(d, title, is_video=True)
        return _xhs_rel(d)
    errs.append("xhs_api.py 兜底也没下到 mp4")
    raise RuntimeError("小红书视频下载失败：" + "；".join(dict.fromkeys(errs)))



def xhs_api_download(nid, real_url, title, d, video=False):
    """走小红书签名接口（xhs_api.py）：图文原图 + 实况 Live 视频一起下。
    成功返回 True。文件命名成 <title>_N.ext / <title>_N.mp4，详情页靠同名自动配对成 Live。
    video=True 时把主视频存成 <title>.mp4（视频笔记用）。"""
    global SRC_TAG
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xhs_api.py")
    if not (XHS_VENV and os.path.isfile(XHS_VENV) and os.path.isfile(script)):
        return False
    tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+=*)", real_url or "")
    url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
    if tok:
        url += f"?xsec_source=app_share&xsec_token={tok.group(1)}"
    tmp = tempfile.mkdtemp(prefix="xhs_api_")
    try:
        env = dict(os.environ, XHS_DIR=XHS_DIR or "", XHS_COOKIE_FILE=XHS_COOKIE_FILE or "")
        r = subprocess.run([XHS_VENV, script, url, tmp], capture_output=True, text=True,
                           timeout=420, env=env)
        out = (r.stdout or "").strip().splitlines()
        data = json.loads(out[-1]) if out and out[-1].startswith("{") else {}
        if not data.get("ok"):
            return False
        got = 0
        for name in data.get("files", []) + data.get("live", []):
            src = os.path.join(tmp, name)
            if not os.path.isfile(src):
                continue
            m = re.search(r"_(\d+)\.[A-Za-z0-9]+$", name)
            idx = m.group(1) if m else str(got + 1)
            ext = name.rsplit(".", 1)[-1].lower()
            if video and ext in ("mp4", "mov") and "_live" not in name.lower():
                dst = os.path.join(d, f"{title}.mp4")
            else:
                dst = os.path.join(d, f"{title}_{idx}.{ext}")
            shutil.move(src, dst)
            got += 1
        if got:
            SRC_TAG = "orig"
            return True
        return False
    except Exception as e:
        log.warning(f"签名接口下载失败: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _img_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"GIF":
        return ".gif"
    return ".jpg"


def _xhs_state_media(note):
    """state 的 imageList → [(图 url, 实况视频 url)]（实况视频没有则为空串）"""
    out = []
    for im in (note.get("imageList") or []):
        if not isinstance(im, dict):
            continue
        url = im.get("urlDefault") or im.get("urlPre") or im.get("url") or ""
        if not url:
            for info in (im.get("infoList") or []):
                if isinstance(info, dict) and info.get("imageScene") == "WB_DFT" and info.get("url"):
                    url = info["url"]
                    break
        if not url:
            for info in (im.get("infoList") or []):
                if isinstance(info, dict) and info.get("url"):
                    url = info["url"]
                    break
        live = ""
        for arr in (im.get("stream") or {}).values():
            if isinstance(arr, list) and arr:
                live = _unesc((arr[0] or {}).get("masterUrl") or (arr[0] or {}).get("url") or "")
                if live:
                    break
        lp = im.get("livePhoto")
        if isinstance(lp, str) and lp.startswith("http"):
            live = _unesc(lp)
        out.append((_unesc(url), live))
    return out


def _finalize_live_pairs(d, title):
    """把目录里已有的实况 mp4（{title}_liveN.mp4 或 {title}_N.mp4，XHS-Downloader/签名
    接口就是这么命名的）与同序号图片配对转成同名 .mov。返回转换个数。"""
    from utils import make_live_photo
    conv = 0
    for p in sorted(glob.glob(os.path.join(d, f"{title}_*.mp4"))):
        name = os.path.basename(p)
        m = re.search(r"_live(\d+)\.mp4$", name, re.I) or re.search(r"_(\d+)\.mp4$", name, re.I)
        if not m:
            continue
        idx = m.group(1)
        img = None
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            cand = os.path.join(d, f"{title}_{idx}{ext}")
            if os.path.isfile(cand):
                img = cand
                break
        if not img:
            continue
        try:
            make_live_photo(img, p)
            conv += 1
        except Exception as e:
            log.warning(f"实况转 mov 失败 {p}: {e}")
    if conv:
        WARN.append(f"{conv} 个实况已转 iPhone Live Photo 格式（同名 .mov）")
    return conv


def xhs_download_images(html, nid, title, author, real_url):
    """图文/实况: 签名接口原图 → 页面 JSON(imageList) 直下 → XHS-Downloader → sns-webpic 兜底。
    Live 图: 配对的 mp4 转成与图同名的 .mov（iPhone 实况格式）"""
    global SRC_TAG
    SRC_TAG = "orig"
    d = os.path.join(MEDIA, "小红书", today(), title)
    os.makedirs(d, exist_ok=True)

    # 1) 优先走签名接口（原图 + 实况 Live）
    if xhs_api_download(nid, real_url, title, d):
        _finalize_live_pairs(d, title)
        gen_thumb(d, title, is_video=False)
        return _xhs_rel(d)

    # 2) 页面 JSON 的 imageList（HTTP 壳页没有，浏览器渲染后才有）
    note = xhs_note_state(html)
    pairs = _xhs_state_media(note)
    if pairs:
        ok, live_pairs = 0, []
        for i, (u, live) in enumerate(pairs, 1):
            if not u:
                continue
            try:
                data = fetch(u, timeout=90, referer="https://www.xiaohongshu.com/")
            except Exception:
                continue
            if len(data) < 1000:
                continue
            with open(os.path.join(d, f"{title}_{i}{_img_ext(data)}"), "wb") as f:
                f.write(data)
            ok += 1
            if live:
                live_pairs.append((i, live))
        if ok:
            _download_xhs_live_photos(live_pairs, d, title)
            gen_thumb(d, title, is_video=False)
            return _xhs_rel(d)
        log.warning("小红书 imageList 里的图全下失败，继续降级")

    # ── 解析 Live 图视频 URL（小红书页面 imageList 里的 livePhoto 字段）──
    # 格式: "livePhotoUrl":"https://.../xxx.mp4" 或 "livePhoto":"..."
    live_urls = re.findall(r'"livePhoto(?:Url)?":"([^"]+\.mp4[^"]*)"', html)
    live_urls = [_unesc(u) for u in live_urls]
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
                return _xhs_rel(d)
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
    return _xhs_rel(d)


def _download_xhs_live_photos(live_urls, d, title):
    """下载小红书 Live 图配对视频，转成 iPhone Live Photo 格式。
    - 下载 mp4 → 转 mov → 注入 Apple 元数据（ContentIdentifier + MediaGroupUUID）
    - 配对图片必须已存在（{title}_{i}.png/jpg/webp）
    - live_urls 支持两种形状：['url', ...] 或 [(图序号, 'url'), ...]（后者能精确配对）
    - 失败不阻塞图文入库（锦上添花）"""
    if not live_urls:
        return
    from utils import make_live_photo
    ok = 0
    converted = 0
    for n, item in enumerate(live_urls[:20], 1):
        if isinstance(item, (tuple, list)) and len(item) == 2:
            i, u = item[0], item[1]
        else:
            i, u = n, item
        # 视频先下到临时 mp4
        tmp_mp4 = os.path.join(d, f"{title}_live{i}.mp4")
        try:
            data = fetch(u, timeout=60, referer="https://www.xiaohongshu.com/")
            if len(data) < 1000:
                continue
            with open(tmp_mp4, "wb") as f:
                f.write(data)
            ok += 1
            # 找配对的图片（XHS-Downloader 存 png，sns-webpic 存 jpg，state 原图可能是 webp）
            img = None
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                cand = os.path.join(d, f"{title}_{i}{ext}")
                if os.path.isfile(cand):
                    img = cand
                    break
            if img:
                # 转成 iPhone Live Photo（mp4→mov + exiftool 注入元数据）
                make_live_photo(img, tmp_mp4)
                converted += 1
            else:
                try:
                    os.remove(tmp_mp4)
                except Exception:
                    pass
        except Exception:
            pass
    if ok > 0:
        msg = f"已下载 {ok} 个 Live 图"
        if converted > 0:
            msg += f"，{converted} 个已转 iPhone Live Photo 格式"
        WARN.append(msg)


def _xhs_rel(d):
    """入库 rel 必须等于磁盘实际目录名：title 可能被 clean_title 改写或为空，
    直接用 title 拼 rel 会让 DB 指向「平台/日期/」这种没有媒体的目录。"""
    return os.path.join("小红书", today(), os.path.basename(d.rstrip("/")))


def download_xiaohongshu(url):
    real, nid, tok, typ = xhs_expand(url)
    html, _ = xhs_get_page(real, nid, tok)
    meta = parse_xhs_meta(html)
    note = xhs_note_state(html)
    ntype = (note.get("type") or "").lower()
    title = clean_title(meta["title"] or "")
    if not title:
        # 视频笔记 state 里 title 常为空，用正文首句兜底
        title = clean_title((note.get("title") or "") or (note.get("desc") or "")) or ""
    title = title or "小红书笔记"
    author = meta["author_name"] or (((note.get("user") or {}).get("nickname")) or "小红书用户")
    avatar = meta["author_avatar"] or ((note.get("user") or {}).get("avatar"))
    cands = xhs_stream_urls(html, nid)
    has_live = bool(re.search(r'"livePhoto"\s*:\s*true', html or ""))
    if ntype:
        is_video = (ntype == "video")          # state 里的 type 最权威
    elif has_live:
        is_video = False                       # 实况/图文笔记里的 mp4 只是 Live 片段
    else:
        is_video = bool(typ == "video" or meta["has_video"] or cands)
    if is_video:
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
    # 实况图取流：轻解析返回的 images[].live_photo_url 恒为空，
    # 改走微博 status 接口（weibo_live.py：游客 cookie → mix_media_info[].video）。
    live_map = {}   # pic_id -> 实况视频 url
    if images and not saved_video:
        try:
            from weibo_live import live_pics
            for _p in live_pics(real):
                if _p.get("video"):
                    live_map[_p["pic_id"]] = _p["video"]
        except Exception as e:
            log.warning(f"微博实况取流失败: {str(e)[:100]}")

    if not saved_video and images:
        for idx, im in enumerate(images, 1):
            short_url = im.get("url") or ""
            live_url = im.get("live_photo_url") or ""
            pid = _wb_pid_from_url(short_url)
            # 直接下原始 URL（large/mw2000 会保留动图），再按文件头决定扩展名。
            # 以前写死 /original/*.jpg → 动图被转成静图。
            orig_url = f"https://wx1.sinaimg.cn/original/{pid}.jpg" if pid else short_url
            blob = None
            for cand in (short_url, orig_url):
                if not cand:
                    continue
                try:
                    r = _OPENER.open(urllib.request.Request(cand, headers={
                        "User-Agent": UA_I, "Referer": "https://weibo.com/",
                    }), timeout=60)
                    b = r.read()
                    if b and len(b) > 1000:
                        blob = b
                        break
                except Exception:
                    continue
            if blob:
                if blob[:4] == b"GIF8":
                    _ext = ".gif"
                elif blob[:8] == b"\x89PNG\r\n\x1a\n":
                    _ext = ".png"
                elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                    _ext = ".webp"
                else:
                    _ext = ".jpg"
                img_path = os.path.join(base, f"{title}_{idx}{_ext}")
                with open(img_path, "wb") as f:
                    f.write(blob)
                saved_n += 1
                # 实况图：下配套视频 → mp4/mov 转同名 .mov（iPhone Live Photo 配对）
                live_v = live_map.get(pid or "") or live_url
                if live_v:
                    try:
                        from weibo_live import download_pic
                        from utils import make_live_photo
                        tmp_mov = os.path.join(base, f".live_{idx}.mov")
                        ok = (download_pic(real, tmp_mov, pid=pid) if pid
                              else download_pic(real, tmp_mov, idx=idx - 1))
                        if ok:
                            make_live_photo(img_path, tmp_mov)
                    except Exception as e:
                        WARN.append(f"微博实况视频失败: {str(e)[:60]}")

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

PROFILE_MAX = int(os.environ.get("PROFILE_MAX", "20"))  # 主页最多展开条数

# ── 真浏览器（Playwright + Chromium）────────────────────
# 抖音/小红书主页都是前端渲染 + 风控，纯 HTTP 拿不到，必须过浏览器
CHROME_PATH = os.environ.get("CHROME_PATH") or "/home/16675244747/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome"

_STEALTH_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome=window.chrome||{runtime:{}};"
    "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
)

# Chromium 在这台机器的 /tmp 上起不来（zygote write: Broken pipe），必须给它一个可用 TMPDIR
BROWSER_TMP = os.environ.get("BROWSER_TMP") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".tmp")

SHORT_HOSTS = ("v.douyin.com", "xhslink.com", "xhslink.cn")


def is_short_url(url: str) -> bool:
    """是不是需要展开的短链（抖音/小红书分享链）"""
    return any(h in url for h in SHORT_HOSTS)


def expand_short_url(url: str, timeout: int = 15) -> str:
    """跟随短链跳转拿真实地址；失败原样返回（不抛错，交给上层判定）"""
    if not is_short_url(url):
        return url
    try:
        r = _OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA_I}), timeout=timeout)
        return r.geturl() or url
    except urllib.error.HTTPError as e:
        return getattr(e, "url", "") or url
    except Exception:
        return url


def _sec_uid_of(u: str) -> str:
    """从抖音主页链接抠 sec_uid（/user/<sec>、/share/user/<sec>）"""
    m = re.search(r"(?:douyin|iesdouyin)\.com/(?:share/)?user/([A-Za-z0-9_\-]+)", u or "")
    return m.group(1) if m else ""


def _xhs_uid_of(u: str) -> str:
    """从小红书主页链接抠 user_id（/user/profile/<id>）"""
    m = re.search(r"xiaohongshu\.com/user/profile/([0-9a-zA-Z]+)", u or "")
    return m.group(1) if m else ""


def canonical_profile_url(url: str) -> str:
    """（可能是短链/share 页）→ 平台标准主页地址；不是主页就原样返回"""
    u = expand_short_url(url)
    sec = _sec_uid_of(u)
    if sec:
        return f"https://www.douyin.com/user/{sec}"
    uid = _xhs_uid_of(u)
    if uid:
        return f"https://www.xiaohongshu.com/user/profile/{uid}"
    return url


def is_profile_url(url: str) -> bool:
    """短链展开后判断真实地址是不是作者主页"""
    u = expand_short_url(url) if is_short_url(url) else url
    if _sec_uid_of(u) or _xhs_uid_of(u):
        return True
    return "tiktok.com/@" in u or "weibo.com/u/" in u


@contextlib.contextmanager
def browser_page(headless=True):
    """起一个 Playwright Chromium（走 PROXY_URL），yield page，退出自动关闭"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"浏览器解析不可用：playwright 未安装（{type(e).__name__}）")
    exe = CHROME_PATH if os.path.isfile(CHROME_PATH) else None
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu", "--lang=zh-CN"]
    try:
        os.makedirs(BROWSER_TMP, exist_ok=True)
    except Exception:
        pass
    env = dict(os.environ)
    env["TMPDIR"] = BROWSER_TMP          # /tmp 上 chromium 起不来，强制换目录
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless, executable_path=exe, args=args, env=env)
        try:
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  f"(KHTML, like Gecko) Chrome/{b.version} Safari/537.36")
            ctx = b.new_context(
                proxy={"server": PROXY_URL} if PROXY_URL else None,
                locale="zh-CN", timezone_id="Asia/Shanghai",
                viewport={"width": 1512, "height": 900}, user_agent=ua,
            )
            ctx.add_init_script(_STEALTH_JS)
            page = ctx.new_page()
            page.set_default_timeout(45000)
            yield page
        finally:
            try:
                b.close()
            except Exception:
                pass


def _item_id_of(url: str) -> str:
    """从单条链接里抠作品 ID（抖音 aweme_id / 小红书 noteId / 微博 bid）"""
    m = re.search(r"/(?:video|note)/(\d{10,})", url or "")
    if m:
        return m.group(1)
    m = re.search(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})", url or "")
    if m:
        return m.group(1)
    m = re.search(r"/detail/(\w{6,})", url or "")
    return m.group(1) if m else ""


def _already_ingested(item_id: str) -> bool:
    """库里是否已经有这个作品（按 ID 模糊匹配 original_url，跨短链/换 token 有效）"""
    if not item_id:
        return False
    try:
        from db import get_conn
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM items WHERE original_url LIKE ? LIMIT 1",
                (f"%{item_id}%",)).fetchone()
            return bool(row)
        finally:
            conn.close()
    except Exception:
        return False



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
    items = []
    for v in videos:
        vid = v.get("video_id") or v.get("id")
        if vid:
            items.append((int(v.get("create_time") or 0), f"https://www.tiktok.com/@{username}/video/{vid}"))
    items.sort(key=lambda x: -x[0])              # 发布时间倒序（最新在前）
    return [u for _, u in items[:PROFILE_MAX]]


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
    items = []
    for card in cards:
        mblog = card.get("mblog")
        if not mblog:
            continue
        bid = mblog.get("bid")
        if not bid:
            continue
        ts = 0
        try:
            import email.utils
            ts = int(email.utils.parsedate_to_datetime(mblog.get("created_at") or "").timestamp())
        except Exception:
            ts = 0
        items.append((ts, f"https://m.weibo.cn/detail/{bid}"))
    items.sort(key=lambda x: -x[0])              # 发布时间倒序（最新在前）
    return [u for _, u in items[:PROFILE_MAX]]


def parse_xhs_profile(url: str) -> List[str]:
    """小红书主页 → 最新 N 条笔记 URL。

    小红书主页必须带 cookie（不带会被弹到 /login），所以顺序是：
      1) cookie + 主页 HTML：页面内嵌 JSON 里有成对的 noteId / xsecToken，直接拼带 token 的笔记链接
      2) cookie 灌进浏览器再读 DOM（拿不到 JSON 时的兜底）
      3) 都没有 cookie → 明确报「需要 cookie」
    """
    user_id = _xhs_uid_of(url)
    if not user_id:
        raise RuntimeError("小红书主页格式错（需 /user/profile/<id>）")
    page_url = f"https://www.xiaohongshu.com/user/profile/{user_id}"

    cookie = ""
    if XHS_COOKIE_FILE and os.path.isfile(XHS_COOKIE_FILE):
        cookie = open(XHS_COOKIE_FILE, encoding="utf-8").read().strip()

    urls: List[str] = []
    seen = set()
    timed: List[tuple] = []   # (发布时间ms, url)，最后按时间倒序取最新 PROFILE_MAX 条

    def _push(nid, tok=None, raw=None, ts=0) -> None:
        nid = nid or ""
        if not re.fullmatch(r"[0-9a-fA-F]{24}", nid) or nid in seen:
            return
        seen.add(nid)
        if raw:
            u = raw.split("#")[0]
        elif tok:
            u = f"https://www.xiaohongshu.com/explore/{nid}?xsec_token={tok}&xsec_source=pc_user"
        else:
            u = f"https://www.xiaohongshu.com/explore/{nid}"
        timed.append((int(ts or 0), u))

    # 1) cookie + HTML
    if cookie:
        try:
            headers = {"User-Agent": UA_D, "Referer": "https://www.xiaohongshu.com/", "Cookie": cookie}
            html = _OPENER.open(urllib.request.Request(page_url, headers=headers),
                                timeout=25).read().decode("utf-8", "ignore")
            for m in re.finditer(
                    r'"noteId":"([0-9a-fA-F]{24})".{0,900}?"xsecToken":"([^"]+)"', html, re.S):
                t = re.search(r'"time":(\d{10,13})', m.group(0))
                _push(m.group(1), m.group(2), ts=t.group(1) if t else 0)
            if not urls and not timed:  # 没配对成功就只取 id（token 缺失可能下载失败，但至少能展开）
                for nid in re.findall(r'"noteId":"([0-9a-fA-F]{24})"', html):
                    _push(nid)
        except Exception as e:
            log.warning(f"小红书主页 HTTP 抓取失败: {type(e).__name__}")

    # 2) 浏览器兜底（cookie 灌进上下文）
    if not timed:
        try:
            with browser_page() as page:
                if cookie:
                    try:
                        page.context.set_extra_http_headers({"Cookie": cookie})
                    except Exception:
                        pass
                page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                page.goto(page_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                for _ in range(5):
                    if len(timed) >= PROFILE_MAX:
                        break
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(1500)
                hrefs = page.eval_on_selector_all(
                    "a[href*='/explore/'], a[href*='/discovery/item/']",
                    "els=>els.map(e=>e.getAttribute('href'))") or []
                html = page.content()
                for m in re.finditer(
                        r'"noteId":"([0-9a-fA-F]{24})".{0,900}?"xsecToken":"([^"]+)"', html, re.S):
                    t = re.search(r'"time":(\d{10,13})', m.group(0))
                    _push(m.group(1), m.group(2), ts=t.group(1) if t else 0)
                for h in hrefs:
                    full = urllib.parse.urljoin("https://www.xiaohongshu.com", h)
                    m = re.search(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})", full)
                    if m:
                        _push(m.group(1), raw=full)
        except Exception as e:
            log.warning(f"小红书主页浏览器解析失败: {e}")

    if timed:
        timed.sort(key=lambda x: -x[0])          # 发布时间倒序 = 最新在前
        return [u for _, u in timed[:PROFILE_MAX]]
    if not cookie:
        raise RuntimeError("小红书主页解析需要 cookie（XHS_COOKIE_FILE 未配置）")
    raise RuntimeError("小红书主页未解析到笔记（cookie 可能已失效，需要在网页端重新登录后更新 cookie）")


def parse_douyin_profile(url: str) -> List[str]:
    """抖音主页 → 最新 N 条视频 URL。

    抖音 web 端 pure HTTP 走不通（aweme/post 需要 a_bogus 签名 + 匿名请求直接返回空体），
    这里用 Playwright Chromium 真浏览器打开主页，同时监听 aweme/post 响应与读 DOM 视频卡片。
    注意：未登录访客抖音只放出约 8 条作品，所以实际条数常小于 PROFILE_MAX。"""
    sec_uid = _sec_uid_of(url)
    if not sec_uid:
        raise RuntimeError("抖音主页无法提取 sec_uid")

    ids: List[str] = []

    def _push(aid) -> None:
        a = str(aid or "").strip()
        if a.isdigit() and len(a) >= 15 and a not in ids:
            ids.append(a)

    hrefs, title, body = [], "", ""
    with browser_page() as page:
        def on_resp(r):
            if "aweme/post" not in r.url:
                return
            try:
                d = json.loads(r.text() or "{}")
            except Exception:
                return
            for aw in (d.get("aweme_list") or []):
                _push(aw.get("aweme_id"))

        page.on("response", on_resp)
        # 先访问首页拿 __ac_nonce/__ac_signature，否则直连用户页会被「验证码中间页」拦下
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        page.goto(f"https://www.douyin.com/user/{sec_uid}", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        for _ in range(6):
            if len(ids) >= PROFILE_MAX:
                break
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
        title = page.title() or ""
        hrefs = page.eval_on_selector_all(
            "a[href*='/video/']", "els=>els.map(e=>e.getAttribute('href'))") or []
        if not ids and not hrefs:
            try:
                body = page.inner_text("body")[:300]
            except Exception:
                pass

    for h in hrefs:
        m = re.search(r"/video/(\d{10,})", h or "")
        if m:
            _push(m.group(1))
    if not ids:
        if "验证" in title or "验证" in body:
            raise RuntimeError("抖音主页解析被风控拦截（验证码页），请稍后重试或换代理节点")
        if "登录" in body:
            raise RuntimeError("抖音主页解析需要登录（页面只给了登录墙）")
        raise RuntimeError(f"抖音主页未解析到视频（{title[:20] or '空白页'}）")
    # aweme_id 是随时间单调递增的雪花 ID，按数值倒序 ≈ 发布时间倒序（最新在前）
    ids.sort(key=lambda x: int(x), reverse=True)
    return [f"https://www.douyin.com/video/{i}" for i in ids[:PROFILE_MAX]]



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
    if _sec_uid_of(url):
        return "douyin"
    if _xhs_uid_of(url):
        return "xiaohongshu"
    if "tiktok.com/@" in url:
        return "tiktok"
    if "weibo.com" in url or "m.weibo.cn/profile" in url:
        return "weibo"
    if "douyin.com/user/" in url:
        return "douyin"
    if "x.com/" in url or "twitter.com/" in url:
        return "x"
    return ""


def process_profile(item):
    """处理主页任务：解析主页 → 拿单条 URL 列表 → 去重 → 加入队列"""
    raw = (item.get("url") or "").strip()
    try:
        url = canonical_profile_url(raw)   # 短链/share 页统一成标准主页地址
    except Exception:
        url = raw
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
        urls = parser(url)[:PROFILE_MAX]   # 各平台统一上限：最新 20 条
        # 去重：库里有（按作品 ID 查，跨短链/换 token 都算重）或队列里已有
        filtered = []
        for u in urls:
            iid = _item_id_of(u)
            if iid and _already_ingested(iid):
                continue
            if get_item_by_url(u):
                continue
            filtered.append(u)
        added = add_sub_tasks(item["id"], filtered, platform)
        item["url"] = url
        item["status"] = "done"
        item["title"] = f"主页展开 {platform}"
        item["message"] = f"解析 {len(urls)} 条，去重后 {len(filtered)} 条已加入队列，新增 {added} 条"
        log.info(f"主页展开 [{platform}] {url}: {len(urls)} 条 → 加入 {added} 条")
    except Exception as e:
        item["status"] = "failed"
        item["message"] = f"主页解析失败: {str(e)[:120]}"
        log.error(f"主页解析失败 [{platform}] {url}: {e}")


def ingest(rel, title, url, platform, author=None, avatar=None, src_tag=None):
    """调用 ingest.py 入库（src_tag 仅用于调用方语义，ingest.py 没有该参数）"""
    args = [PY, INGEST, f"--platform={platform}", f"--url={url}", f"--path={rel}", f"--title={title}"]
    if author and author != "未知作者":
        args.append(f"--author={author}")
    if avatar:
        args.append(f"--avatar={avatar}")
    r = subprocess.run(args, capture_output=True, timeout=60)
    out = (r.stdout or b"").decode("utf-8", "ignore")
    err = (r.stderr or b"").decode("utf-8", "ignore")
    if "DUP " in out or "已收藏过" in out or "内容重复" in out or "跳过" in out:
        # 重复内容：文件移到回收目录，绝不 rm（用户要求可回溯）
        d = os.path.join(MEDIA, rel)
        referenced = False
        try:
            from db import is_path_referenced
            referenced = is_path_referenced(rel)
        except Exception:
            referenced = True
        if os.path.isdir(d) and not referenced:
            dst = trash_duplicate(d, platform, tag="dup")
            if dst:
                log.info(f"♻️ 重复目录已移入回收站: {dst}")
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
    try:
        from ig_downloader import download_instagram, IGNoCookie
    except Exception:  # 模块缺失不影响其他平台
        download_instagram = None

        class IGNoCookie(RuntimeError):
            pass
    url = item["url"].strip()
    if not url.startswith("http"):
        item["status"] = "failed"
        item["message"] = "无效链接"
        return
    # 主页任务走单独流程
    if item.get("type") == "profile":
        process_profile(item)
        return
    # 短链可能指向作者主页（不是单条视频）：展开后分流，别直接判死
    if is_short_url(url):
        real = expand_short_url(url)
        if is_profile_url(real):
            item["type"] = "profile"
            item["url"] = canonical_profile_url(real) or real
            log.info(f"短链识别为作者主页 → 主页流程: {url} → {item['url']}")
            process_profile(item)
            return
    # 直接贴的主页地址（iesdouyin/share/user、xiaohongshu/user/profile 等）也走主页流程
    if item.get("type") != "profile" and (_sec_uid_of(url) or _xhs_uid_of(url)):
        item["type"] = "profile"
        item["url"] = canonical_profile_url(url)
        log.info(f"识别为作者主页 → 主页流程: {url} → {item['url']}")
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
        elif "instagram.com" in url:
            rel, title, plat, author = download_instagram(url)
        else:
            item["status"] = "failed"
            item["message"] = "不支持的平台(支持抖音/小红书/X/微博/TikTok/Instagram)"
            return
        ingest(rel, title, item.get("original_url") or url, plat,
               author=author or None, avatar=avatar if plat == "小红书" else None,
               src_tag=SRC_TAG)
        # 兜底：入库时目录可能还没写完（缩略图/原图后到），补一次封面
        try:
            subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_covers.py")],
                           capture_output=True, timeout=30)
        except Exception as _e:
            log.warning(f"封面回填失败: {_e}")
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
    except IGNoCookie as e:
        # 缺 cookie 属配置问题，重试无意义：直接 failed
        item["status"] = "failed"
        item["message"] = str(e)
        log.error(f"处理失败 [{url}]: {e}")
        _notify_failure(url, str(e)[:100])
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
    from queue_manager import cleanup_queue, update_item, remove_items
    cleanup_queue()
    q = load_queue()
    if not q:
        return

    target = next((it for it in q if it.get("status") == "pending"), None)
    if not target:
        return
    # 只改这一条，不再整表回写（否则会覆盖用户/主页展开刚加进来的任务）
    update_item(target["id"], {"status": "processing"})
    process_one(target)
    if target.get("status") == "done":
        remove_items([target["id"]])
    else:
        update_item(target["id"], {k: target.get(k) for k in (
            "status", "message", "retries", "type", "url", "title", "path")})


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
