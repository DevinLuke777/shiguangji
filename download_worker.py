#!/usr/bin/env python3
"""
拾光集 · 后台自动入库队列 worker（2026-08-18）
从 拾光集/_queue.json 读取 pending 链接，自动:
  抖音    -> 轻解析(8086, cookie已配) 解析下载 -> 归档 -> ingest
  小红书视频 -> 短链展开 -> 挖 258 无水印流 -> 下载 -> 归档 -> ingest
  小红书图文 -> XHS-Downloader + cookie 下无水印原图 -> 归档 -> ingest
               (token 过期则降级 sns-webpic 带水印渲染图并标记 degraded)
用法: python3 download_worker.py --drain    # 处理一次队列里所有 pending
用系统/Hermes cron 每 2 分钟调用一次; stdout 为空=静默(成功不打扰)。
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request, urllib.parse, shutil, glob
from datetime import datetime

MEDIA = os.environ.get("MEDIA_ROOT", "/vol1/1000/Downloads/拾光集")   # 换成你的媒体根目录
QUEUE = os.environ.get("QUEUE_FILE", os.path.join(MEDIA, "_queue.json"))
LOCK = os.path.join(MEDIA, "_queue.lock")
VALT = os.environ.get("INGEST_DIR", "/vol1/1000/Docker/shiguangji")  # ingest.py 所在(运行实例)
INGEST = os.path.join(VALT, "ingest.py")
# 路径迁移：原飞牛 Hermes 已弃用，改用本地 Hermes venv
PY = os.environ.get("WORKER_PYTHON", "/home/16675244747/.hermes/hermes-agent/venv/bin/python3")
XHS_VENV = os.environ.get("XHS_PYTHON", "/home/16675244747/.hermes/workspace/xhs-downloader/.venv/bin/python")  # XHS-Downloader 独立 venv

UA_I = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
UA_D = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


# ── 队列读写 ──────────────────────────────────────────
def load_queue():
    if not os.path.isfile(QUEUE):
        return []
    try:
        with open(QUEUE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_queue(q):
    tmp = QUEUE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走代理
WARN = []  # 本次下载的警告(如水印兜底)，由 process_one 写进队列消息


def fetch(url, timeout=20, referer=None):
    headers = {"User-Agent": UA_I}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    # 直连：本机服务(localhost:8086 轻解析)若走 http_proxy 会被 sing-box 劫持报 502 Bad Gateway
    return _OPENER.open(req, timeout=timeout).read()


def clean_title(t):
    t = (t or "").strip()
    while t and not re.match(r"[\u4e00-\u9fffA-Za-z0-9]", t[0]):
        t = t[1:]
    # 去换行符（0x0a/0x0d -> 空格），否则目录名带换行 Flask URL 404
    t = re.sub(r'[\n\r]+', ' ', t).strip()
    # 去掉末尾 #话题标签（小红书常见）
    t = re.sub(r'(\s*#\S+\s*)+$', '', t).strip()
    # 截断过长标题（目录名限制）
    if len(t) > 80:
        t = t[:80].rstrip()
    return t


def today():
    return time.strftime("%Y-%m-%d")


def gen_thumb(d, title, is_video):
    """生成 _thumb.jpg"""
    try:
        src = os.path.join(d, f"{title}.mp4") if is_video else os.path.join(d, f"{title}_1.png")
        if not os.path.isfile(src):
            # 图文可能用 jpg; 图片多命名时兜底
            cands = sorted(glob.glob(os.path.join(d, "*.png")) + glob.glob(os.path.join(d, "*.jpg")) + glob.glob(os.path.join(d, f"{title}_1.jpeg")))
            if not cands:
                return
            src = cands[0]
        cmd = ["ffmpeg", "-y"] + (["-ss", "0.5"] if is_video else []) + \
            ["-i", src, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", os.path.join(d, "_thumb.jpg")]
        subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        pass


# ── 抖音 ──────────────────────────────────────────────
def download_douyin(url):
    """轻解析解析+下载，返回 (relative_path, title, platform, author)。
    三重串号防护:
      1) 调轻解析前清掉抖音目录下已存在的旧 untitled 残留(轻解析复用同名 untitled 是串号根源)
      2) 用时间戳快照只认"本次触发后新出现"的落盘目录
      3) 认领后立即把 untitled 改名到标题目录(切断下次复用污染)"""
    # 展开短链
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
                real = urllib.request.urlopen(urllib.request.Request(short.group(0), headers={"User-Agent": UA_I}), timeout=15).geturl()
            except Exception:
                real = url
    base = os.path.join(MEDIA, "抖音", today())
    os.makedirs(base, exist_ok=True)
    # 防护1: 清掉已存在的旧 untitled 残留（防止轻解析复用同名目录污染本次下载）
    old_untitled = os.path.join(base, "untitled")
    if os.path.isdir(old_untitled):
        try:
            shutil.rmtree(old_untitled)
        except Exception:
            pass
    # 防护2: 调轻解析前记录目录快照
    pre_names = set(os.listdir(base))
    started = time.time()
    # 调轻解析
    enc = urllib.parse.quote(real, safe="")
    api = f"http://localhost:8086/video/share/url/parse?url={enc}"
    d = json.loads(fetch(api, timeout=40).decode("utf-8", "ignore"))
    if d.get("code") != 200:
        raise RuntimeError(f"轻解析失败: {d.get('msg','?')}")
    data = d.get("data") or {}
    author = (data.get("author") or {}).get("name") or "未知作者"
    title = clean_title(data.get("title")) or author
    # 等落盘: 只认"本次新出现"的目录（快照外的），或**解析后被刷新的既有同名目录**
    #（轻解析复用同名目录时不会出现在 new 里 → 只认 new 会误报「等待抖音落盘超时」）
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
        # 兜底：新的 untitled（本次新建的）
        if os.path.isdir(old_untitled):
            files = [f for f in os.listdir(old_untitled)]
            if any(f.lower().endswith((".mp4", ".mov", ".jpg", ".jpeg", ".png")) for f in files):
                found = old_untitled
    if not found:
        raise RuntimeError("等待抖音落盘超时")
    # 防护3: 归档到 抖音/日期/标题/。
    # 关键修复: 标题目录若已存在(多条不同视频可能同名, 如空标题都用作者名),
    #   **绝不删除**——追加唯一后缀(_2/_3)并存，避免覆盖/删掉别人的内容
    base_dir = "".join(c for c in title if c not in ':：/\\*?"<>|')[:60] or f"抖音_{int(time.time())}"
    target = os.path.join(base, base_dir)
    suffix = 2
    while os.path.isdir(target):
        target = os.path.join(base, f"{base_dir}_{suffix}")
        suffix += 1
    os.makedirs(target, exist_ok=True)
    # ★核心修复: final_dir = 实际创建的目录名(可能带 _2/_3 后缀)。
    #   rel(入库path) 和 文件名前缀 都必须用它，与下载目录严格一致
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
    # 清空源目录
    if os.path.isdir(found) and os.listdir(found) == []:
        try:
            os.rmdir(found)
        except Exception:
            pass
    rel = os.path.join("抖音", today(), final_dir)
    d = os.path.join(MEDIA, rel)
    # 判断图文: 有图片且无视频(true video 帖会 moved_video)
    is_图文 = (not moved_video) and any(f.lower().endswith((".jpg", ".jpeg", ".png")) for f in os.listdir(d))
    gen_thumb(d, final_dir, is_video=not is_图文)
    return rel, title, "抖音", author


# ── 小红书 ─────────────────────────────────────────────
def xhs_expand(url):
    """展开 xhslink 短链，返回 (real_url, note_id, xsec_token, type)"""
    r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA_I}), timeout=15)
    real = r.geturl()
    m = re.search(r"/discovery/item/([a-f0-9]{24})", real)
    if not m:
        raise RuntimeError("小红书短链展开失败")
    nid = m.group(1)
    # token 在展开后的 real URL 里(xhslink短链本身不含)，其次才可能从原url带
    tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", real)
    if not tok:
        tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", url or "")
    tok = tok.group(1) if tok else None
    # type 从 real URL 的 type=video / type=normal
    typ = "normal"
    tm = re.search(r"type=(\w+)", real)
    if tm:
        typ = tm.group(1)
    return real, nid, tok, typ


def xhs_get_page(real_url, nid, tok):
    """抓页面。实测(2026-08-18): 带 cookie 反而触发反爬返回 ~10KB JS 壳，
    不带 cookie + token + 桌面UA 成功(861KB 含 masterUrl)。token 失效时返回空壳→抛错。
    2026-09-14 补：先不带 cookie 试，失败再带 cookie（cookie 通道可救部分 token 过期页）。"""
    url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
    if tok:
        url += f"?xsec_source=app_share&xsec_token={tok}="
    cookie_file = os.environ.get("XHS_COOKIE_FILE", "/home/16675244747/.hermes/workspace/xhs_cookie.txt")
    cookie = open(cookie_file, encoding="utf-8").read().strip() if os.path.isfile(cookie_file) else ""
    passes = [(UA_D, ""), (UA_I, "")]
    if cookie:
        passes += [(UA_I, cookie), (UA_D, cookie)]
    for ua, ck in passes:
        try:
            headers = {"User-Agent": ua, "Referer": "https://www.xiaohongshu.com/"}
            if ck:
                headers["Cookie"] = ck
            req = urllib.request.Request(url, headers=headers)
            html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            if len(html) > 10000 and ("masterUrl" in html or "imageList" in html or ("nickname" in html and '"title"' in html)):
                return html, nid
        except Exception:
            pass
    raise RuntimeError("小红书页面抓取失败(可能是 xsec_token 过期,需在App重新分享该链接)")


def xhs_pick_stream(html):
    """挑无水印流：优先 309(X265) → 258(X264) → 兜底第一个 masterUrl(通常 259 带水印)。
    返回 (url, tag)。259 流带作者头像+小红书 logo 水印，309/258 是干净原流(2026-09-14 实锤：
    本地字节数 == 页面 259 流 Content-Length 完全一致)。"""
    for tag in ("309", "258"):
        m = re.search(r'"masterUrl":"([^"]+_%s\.mp4[^"]*)"' % tag, html)
        if m:
            return m.group(1).replace("\\u002F", "/").replace("\\/", "/"), tag
    m = re.search(r'"masterUrl":"([^"]+)"', html)
    if not m:
        return None, None
    return m.group(1).replace("\\u002F", "/").replace("\\/", "/"), "259"


def xhs_downloader_video(nid, real_url, title, d):
    """用 XHS-Downloader 下视频(它走官方签名接口，稳定拿 309 无水印流)。
    成功返回 True。"""
    cookie_file = os.environ.get("XHS_COOKIE_FILE", "/home/16675244747/.hermes/workspace/xhs_cookie.txt")
    if not (os.path.isfile(cookie_file) and os.path.isfile(XHS_VENV)):
        return False
    tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", real_url or "")
    url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
    if tok:
        url += f"?xsec_source=app_share&xsec_token={tok.group(1)}="
    work = f"/tmp/xhs_vid_{nid}"
    shutil.rmtree(work, ignore_errors=True)
    try:
        xhs_dir = os.environ.get("XHS_DIR", "/home/16675244747/.hermes/workspace/xhs-downloader")
        cookie = open(cookie_file, encoding="utf-8").read().strip()
        subprocess.run([XHS_VENV, os.path.join(xhs_dir, "main.py"), "--url", url,
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
    """挖无水印流(309/258)下载视频；页面只有 259 时改走 XHS-Downloader。"""
    vurl, tag = xhs_pick_stream(html)
    if not vurl:
        raise RuntimeError("无 masterUrl")
    d = os.path.join(MEDIA, "小红书", today(), title)
    os.makedirs(d, exist_ok=True)
    if tag == "259":
        # 页面只给水印流 → 先试 XHS-Downloader(签名接口常能拿到 309)
        if xhs_downloader_video(nid, real_url, title, d):
            gen_thumb(d, title, is_video=True)
            return os.path.join("小红书", today(), title)
    if tag == "259":
        WARN.append("平台只提供 259 水印流(无 309/258)，已入库但可能带水印，可在 App 重新分享后重试")
    data = fetch(vurl, timeout=120, referer="https://www.xiaohongshu.com/")
    with open(os.path.join(d, f"{title}.mp4"), "wb") as f:
        f.write(data)
    gen_thumb(d, title, is_video=True)
    return os.path.join("小红书", today(), title)


def xhs_download_images(html, nid, title, author, real_url):
    """图文: 优先 XHS-Downloader 无水印原图; 失败降级 sns-webpic 直抓"""
    d = os.path.join(MEDIA, "小红书", today(), title)
    os.makedirs(d, exist_ok=True)
    # 尝试 XHS-Downloader(无水印原图)
    cookie_file = os.environ.get("XHS_COOKIE_FILE", "/home/16675244747/.hermes/workspace/xhs_cookie.txt")
    if os.path.isfile(cookie_file) and os.path.isfile(XHS_VENV):
        tok = re.search(r"xsec_token=([A-Za-z0-9_\-]+)=", real_url or "")
        url = f"https://www.xiaohongshu.com/discovery/item/{nid}"
        if tok:
            url += f"?xsec_source=app_share&xsec_token={tok.group(1)}="
        cookie = open(cookie_file, encoding="utf-8").read().strip()
        work = f"/tmp/xhs_worker_{nid}"
        shutil.rmtree(work, ignore_errors=True)
        try:
            xhs_dir = os.environ.get("XHS_DIR", "/home/16675244747/.hermes/workspace/xhs-downloader")
            subprocess.run(
                [XHS_VENV, os.path.join(xhs_dir, "main.py"),
                 "--url", url, "--cookie", cookie, "--work_path", work,
                 "--image_format", "PNG", "--download_record", "false"],
                capture_output=True, timeout=150)
            pngs = glob.glob(os.path.join(work, "Download", "**", "*.png"), recursive=True)
            # 递归 glob 已覆盖根目录，额外去重（同一文件被两条 glob 命中会导致每张图存两份）
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
                gen_thumb(d, title, is_video=False)
                return os.path.join("小红书", today(), title)
        except Exception:
            pass
    # 降级: sns-webpic 直抓(带水印)
    urls = re.findall(r'https?://sns-webpic[^"\\s\\]|\\"\\'']+', html)
    urls = [u for u in urls if "h5_1080" in u or "!nd_prv" in u or "!nd_dft" in u]
    seen, picked = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            picked.append(u.replace("\\u002F", "/"))
    if not picked:
        # 裸 http URL
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
    gen_thumb(d, title, is_video=False)
    return os.path.join("小红书", today(), title)


def download_xiaohongshu(url):
    real, nid, tok, typ = xhs_expand(url)
    html, _ = xhs_get_page(real, nid, tok)
    # 提取标题：优先 <title>（权威「笔记标题 - 小红书」），再 note JSON，最后 fallback
    mt = re.search(r'<title>([^<]*?)</title>', html)
    title = None
    if mt:
        t = mt.group(1).replace(" - 小红书", "").strip()
        if t and t != "小红书":
            title = t
    if not title:
        mn = re.search(r'"title":"([^"]*)"', html)
        if mn and mn.group(1) and "想了解" not in mn.group(1):
            title = mn.group(1)
    title = clean_title(title or "小红书笔记")
    author = "小红书用户"
    ma = re.search(r'"nickname":"([^"]*)"', html)
    if ma:
        author = ma.group(1)
    # 提取头像（解码 \u002F 转义）
    avatar = None
    av_m = re.search(r'"avatar":"([^"]+)"', html)
    if av_m:
        avatar = av_m.group(1).replace("\\u002F", "/").replace("\u002F", "/")
    has_video = bool(re.search(r'"masterUrl":"[^"]+_(259|309|258)\.mp4', html))
    if typ == "video" or has_video or ('"masterUrl"' in html and '"imageList"' not in html):
        rel = xhs_download_video(html, nid, title, author, real_url=real)
        return rel, title, "小红书", avatar
    else:
        rel = xhs_download_images(html, nid, title, author, real)
        return rel, title, "小红书", avatar


# ── 主流程 ─────────────────────────────────────────────

# ── X / Twitter (2026-09-09) ──────────────────────────────
def download_x(url):
    """X/Twitter 链接: fxtwitter API 解析 → 桌面 UA + 代理下载 → 归档 → ingest
    走 飞牛 trick: 不依赖 yt-dlp(反爬+amplify_video 不认),用 fxtwitter 拿 metadata + 最高码率视频流。
    """
    import re as _re
    url = _re.sub(r'/video/\d+', '', url)  # 去掉 /video/1 后缀
    m = _re.search(r'/([^/]+)/status/(\d+)', url)
    if not m:
        raise RuntimeError("X 链接格式错(需 /<user>/status/<id>)")
    user, tid = m.group(1), m.group(2)
    api = f"https://api.fxtwitter.com/{user}/status/{tid}"
    with urllib.request.urlopen(api, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    tweet = d.get("tweet") or {}
    text = (tweet.get("text") or "").strip()
    author = (tweet.get("author") or {}).get("name") or "未知作者"
    media_all = (tweet.get("media") or {}).get("all") or []
    if not media_all:
        raise RuntimeError("X 推文无媒体")

    # 标题优先级
    if text:
        title = clean_title(text[:60]) or f"{author}_{tid[-8:]}"
    else:
        title = f"{author}_{tid[-8:]}"

    # 选最高码率视频 or 第一张图
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

    # 下载（必须走代理 + 桌面 UA，否则 video.twimg.com 国内被墙）
    UA_D = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    proxy_handler = urllib.request.ProxyHandler({"http": "http://127.0.0.1:20172",
                                                  "https": "http://127.0.0.1:20172"})
    opener = urllib.request.build_opener(proxy_handler)
    opener.addheaders = [("User-Agent", UA_D)]
    try:
        with opener.open(vurl, timeout=120) as r:
            data = r.read()
    except Exception:
        # 直连兜底
        with urllib.request.urlopen(urllib.request.Request(vurl, headers={"User-Agent": UA_D}), timeout=120) as r:
            data = r.read()
    with open(path, "wb") as f:
        f.write(data)

    gen_thumb(d, title, is_video=(ext == "mp4"))
    return os.path.join("X", today(), title), title, "X", author


def ingest(rel, title, url, platform, author=None, avatar=None):
    # ⚠ 参数一律用 --opt=value 形式：作者名可能以「-」开头(如 -哇咔咔咔咔)，
    #   用空格分隔时 argparse 会把它当选项 → "expected one argument" → 入库静默失败
    args = [PY, INGEST, f"--platform={platform}", f"--url={url}", f"--path={rel}", f"--title={title}"]
    if author and author != "未知作者":
        args.append(f"--author={author}")
    if avatar:
        args.append(f"--avatar={avatar}")
    r = subprocess.run(args, capture_output=True, timeout=60)
    out = (r.stdout or b"").decode("utf-8", "ignore")
    err = (r.stderr or b"").decode("utf-8", "ignore")
    # 去重拦截或被跳过：下载的文件不会入库 → 清掉已下载目录防残留
    # ⚠ 安全阀：若该目录已被库里某条目引用（轻解析会复用同名目录覆盖下载），绝不能删，
    #   否则会把已收藏条目的媒体一起删掉（出现「有记录无文件」）
    if "已收藏过" in out or "跳过" in out:
        d = os.path.join(MEDIA, rel)
        referenced = False
        try:
            import sqlite3 as _sq
            _c = _sq.connect(os.path.join(VALT, "media_library.db"))
            referenced = bool(_c.execute("SELECT 1 FROM items WHERE local_path=?", (rel,)).fetchone())
            _c.close()
        except Exception:
            referenced = True  # 查不到就保守当作已引用，宁可不删
        if os.path.isdir(d) and not referenced:
            shutil.rmtree(d, ignore_errors=True)
        return False
    if r.returncode != 0:
        raise RuntimeError(f"入库失败({r.returncode}): {(err or out).strip()[:150]}")
    return True


def process_one(item):
    url = item["url"].strip()
    if not url.startswith("http"):
        item["status"] = "failed"
        item["message"] = "无效链接"
        return
    try:
        author = None
        if "douyin" in url or "iesdouyin" in url:
            rel, title, plat, author = download_douyin(url)
        elif "xhslink" in url or "xiaohongshu" in url:
            rel, title, plat, avatar = download_xiaohongshu(url)
        elif "x.com" in url or "twitter.com" in url:
            rel, title, plat, author = download_x(url)
        else:
            item["status"] = "failed"
            item["message"] = "不支持的平台(仅支持抖音/小红书/X)"
            return
        ingest(rel, title, item.get("original_url") or url, plat, author=author or None, avatar=avatar if plat == "小红书" else None)
        item["status"] = "done"
        item["title"] = title
        item["path"] = rel
        if WARN:
            item["message"] = "⚠️ " + "; ".join(WARN)
            WARN.clear()
        else:
            item["message"] = "完成"
    except Exception as e:
        item["status"] = "failed"
        item["message"] = str(e)[:150]


def drain():
    q = load_queue()
    if not q:
        return
    # 清理：done(成功已入库)清掉；failed 保留1天(超时自动清)；pending/processing保留
    now_ts = time.time()
    kept = []
    for it in q:
        st = it.get("status")
        if st == "done":
            if str(it.get("message", "")).startswith("⚠️"):
                # 成功但带警告(如水印兜底)：保留 1 天让用户在队列页看到
                try:
                    ct = datetime.strptime(it.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
                    if now_ts - ct.timestamp() > 86400:
                        continue
                except Exception:
                    pass
                kept.append(it)
                continue
            continue  # 成功直接删
        if st == "failed":
            # 超过1天自动清（86400s），保留原因供短期查看
            try:
                ct = datetime.strptime(it.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
                if now_ts - ct.timestamp() > 86400:
                    continue
            except Exception:
                pass  # created_at 无法解析则保留
        kept.append(it)
    if len(kept) != len(q):
        q = kept
        save_queue(q)
    if not q:
        return
    changed = False
    # 只把第一个 pending 标记为 processing(其余保持 pending, 下次周期再处理)
    for item in q:
        if item.get("status") == "pending":
            item["status"] = "processing"
            changed = True
            break
    if changed:
        save_queue(q)
    # 处理第一个 processing, 立即保存
    for item in q:
        if item.get("status") == "processing":
            process_one(item)
            # 成功的立即从队列移除(已入库)；失败的保留(原因让用户看)
            if item.get("status") == "done":
                q = [it for it in q if it.get("id") != item.get("id")]
            save_queue(q)
            break  # 每次调用只处理一个，防超时; 下个周期处理下一个


def main():
    # cron no_agent 直接跑脚本(不带参数) → 默认执行 drain; --drain 仅是显式同效
    # 简单锁防并发
    if os.path.isfile(LOCK):
        try:
            age = time.time() - os.path.getmtime(LOCK)
            if age < 90:
                return  # 上次还在跑(超90s才允许重入)
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
