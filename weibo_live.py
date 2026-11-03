#!/usr/bin/env python3
"""微博实况图（Live Photo）取流。

问题背景：轻解析（lite.parser /video/share/url/parse）返回的
images[].live_photo_url 恒为空，拿不到实况视频。

可行途径（已实测）：
  1. 用 genvisitor2 换一对游客 cookie（SUB/SUBP）
  2. GET https://weibo.com/ajax/statuses/show?id=<status_id>  （带 cookie）
  3. 帖子媒体在 data.mix_media_info.items[].data：
        type == "livephoto" 时该 pic 就是实况图，
        data["video"] 形如
        https://video.weibo.com/media/play?livephoto=<urlencode(https://us.sinaimg.cn/xxx.mov)>&Expires=..&ssig=..
     us.sinaimg.cn 裸链会 403（缺签名），必须走 video.weibo.com/media/play，
     它 302 到 livephoto.us.sinaimg.cn 的真实 .mov（本身就是 QuickTime MOV）。
  4. 老接口 data.pic_infos[pid]["video"] 同名字段，作为兜底。

对外接口：
  live_pics(url_or_id) -> [{pic_id, still, video, w, h, live}]   按微博展示顺序
  download_video(url, dest) -> bool                              校验 > 100KB 且是 MOV/MP4
  match_local_jpgs(dirpath, pics) -> {pic_index: 本地jpg路径}      按图片内容指纹配对
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")

_BASE = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(_BASE, ".tmp", "weibo_visitor.json")
_TTL = 6 * 3600

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read(), r.geturl()


# ── 游客 cookie ──────────────────────────────────────────
def _cookie(force=False):
    """返回 'SUB=..; SUBP=..'，带文件缓存。"""
    if not force:
        try:
            with open(_CACHE) as f:
                c = json.load(f)
            if time.time() - c.get("ts", 0) < _TTL and c.get("cookie"):
                return c["cookie"]
        except Exception:
            pass
    data, _ = _http("https://passport.weibo.com/visitor/genvisitor2"
                    "?cb=visitor_gray_callback&tid=&from=weibo",
                    {"User-Agent": UA}, timeout=20)
    txt = data.decode("utf-8", "ignore")
    m = re.search(r"visitor_gray_callback\((\{.*?\})\)", txt, re.S)
    if not m:
        raise RuntimeError("genvisitor2 解析失败")
    d = json.loads(m.group(1)).get("data") or {}
    sub, subp = d.get("sub"), d.get("subp")
    if not sub:
        raise RuntimeError("genvisitor2 未返回 sub")
    cookie = f"SUB={sub}; SUBP={subp}" if subp else f"SUB={sub}"
    try:
        os.makedirs(os.path.dirname(_CACHE), exist_ok=True)
        with open(_CACHE, "w") as f:
            json.dump({"ts": time.time(), "cookie": cookie}, f)
    except Exception:
        pass
    return cookie


def status_id(url_or_id):
    """从微博链接或裸 id 里取 status id。"""
    s = str(url_or_id or "").strip()
    m = re.search(r"/(?:status|detail)/([0-9A-Za-z]+)", s)
    if m:
        s = m.group(1)
    else:
        s = s.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return s


# ── 帖子 JSON ──────────────────────────────────────────
def get_status(url_or_id, nocache=False):
    sid = status_id(url_or_id)
    if not re.fullmatch(r"[0-9A-Za-z]+", sid or ""):
        raise RuntimeError(f"无法识别微博 status id: {url_or_id}")
    last = None
    for attempt in range(2):
        try:
            q = f"https://weibo.com/ajax/statuses/show?id={sid}"
            if nocache:
                q += f"&_={int(time.time() * 1000)}"
            body, _ = _http(
                q,
                {"User-Agent": UA, "Referer": f"https://weibo.com/{sid}",
                 "Cookie": _cookie(force=attempt > 0),
                 "Accept": "application/json, text/plain, */*"},
                timeout=25)
            d = json.loads(body.decode("utf-8", "ignore"))
            if isinstance(d, dict) and (d.get("id") or d.get("mid")
                                        or d.get("mix_media_info")
                                        or d.get("pic_infos")):
                # 转发帖：媒体在 retweeted_status 里
                if not (d.get("mix_media_info") or d.get("pic_infos")) and d.get("retweeted_status"):
                    return d["retweeted_status"]
                return d
            last = d
        except Exception as e:
            last = e
        time.sleep(1)
    raise RuntimeError(f"微博 status 接口失败: {str(last)[:120]}")


def _video_of(pic):
    v = pic.get("video") or ""
    if isinstance(v, dict):
        v = v.get("url") or v.get("src") or ""
    if v and not v.startswith("http"):
        v = "https:" + v if v.startswith("//") else ""
    return v


def _status_m(url_or_id, nocache=False):
    """m.weibo.cn 版帖子 JSON（data 部分）。需要游客 cookie。"""
    sid = status_id(url_or_id)
    if not re.fullmatch(r"[0-9A-Za-z]+", sid or ""):
        raise RuntimeError(f"无法识别微博 status id: {url_or_id}")
    q = f"https://m.weibo.cn/statuses/show?id={sid}"
    if nocache:
        q += f"&_={int(time.time() * 1000)}"
    body, _ = _http(q, {"User-Agent": UA, "Cookie": _cookie(),
                        "Referer": "https://m.weibo.cn/"}, timeout=25)
    d = json.loads(body.decode("utf-8", "ignore"))
    if d.get("ok") != 1 or not isinstance(d.get("data"), dict):
        raise RuntimeError("m.weibo.cn 返回异常")
    data = d["data"]
    if not (data.get("pics") or data.get("pic_ids")) and data.get("retweeted_status"):
        return data["retweeted_status"]
    return data


def live_pics(url_or_id, nocache=False):
    """按展示顺序返回 [{pic_id, still, small, video, w, h, live}]。

    优先 m.weibo.cn/statuses/show（data.pics[] 最完整，视频条目也在里面，
    顺序与网页展示一致）；失败再退回 weibo.com/ajax/statuses/show 的
    mix_media_info / pic_infos。
    """
    d = None
    try:
        d = _status_m(url_or_id, nocache=nocache)
        if not (d.get("pics") or d.get("pic_ids")):
            d = None
    except Exception:
        d = None
    if d:
        out = []
        for p in (d.get("pics") or []):
            pid = p.get("pid")
            typ = p.get("type")
            if not pid or typ == "video":   # 纯视频条目，不是实况图
                continue
            large = (p.get("large") or {}).get("url") or ""
            v = p.get("videoSrc") or ""
            out.append({
                "pic_id": pid,
                "still": large or p.get("url") or "",
                "small": p.get("url") or large,
                "video": v,
                "live": typ == "livephoto" and bool(v),
                "w": (p.get("large") or {}).get("geo", {}).get("width"),
                "h": (p.get("large") or {}).get("geo", {}).get("height"),
            })
        if out:
            return out
    return _live_pics_web(url_or_id, nocache=nocache)


def _live_pics_web(url_or_id, nocache=False):
    """weibo.com 版兜底：mix_media_info（含 livephoto 的 video 字段）。"""
    d = get_status(url_or_id, nocache=nocache)
    out = []
    items = ((d.get("mix_media_info") or {}).get("items")) or []
    for it in items:
        if (it.get("type") not in (None, "pic")):
            continue
        p = it.get("data") or {}
        pid = p.get("pic_id")
        if not pid:
            continue
        still = ((p.get("largest") or {}).get("url")
                 or (p.get("original") or {}).get("url")
                 or (p.get("large") or {}).get("url") or "")
        small = ((p.get("thumbnail") or {}).get("url")
                 or (p.get("bmiddle") or {}).get("url") or still)
        v = _video_of(p)
        out.append({
            "pic_id": pid,
            "still": still,
            "small": small,
            "video": v,
            "live": bool(v) or p.get("type") == "livephoto",
            "w": (p.get("largest") or {}).get("width"),
            "h": (p.get("largest") or {}).get("height"),
        })
    if not out:  # 老结构兜底
        infos = d.get("pic_infos") or {}
        for pid in (d.get("pic_ids") or list(infos)):
            p = infos.get(pid) or {}
            if not p:
                continue
            still = ((p.get("largest") or {}).get("url")
                     or (p.get("original") or {}).get("url") or "")
            v = _video_of(p)
            out.append({"pic_id": pid, "still": still,
                        "small": ((p.get("thumbnail") or {}).get("url") or still),
                        "video": v, "live": bool(v),
                        "w": (p.get("largest") or {}).get("width"),
                        "h": (p.get("largest") or {}).get("height")})
    return out


# ── 视频下载 ──────────────────────────────────────────
def _is_media(blob):
    return (blob[:8] == b"\x89PNG\r\n\x1a\n" or blob[:2] == b"\xff\xd8"
            or blob[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free")
            or blob[:3] == b"GIF")


def download_video(url, dest, timeout=120, min_size=100 * 1024):
    """下载实况视频到 dest；返回 True 表示已落盘且校验通过。"""
    cands = [url]
    # 兜底：解开 media/play?livephoto= 里的真实地址
    m = re.search(r"[?&]livephoto=([^&]+)", url or "")
    if m:
        inner = urllib.parse.unquote(m.group(1))
        if inner not in cands:
            cands.append(inner)
    for u in cands:
        try:
            blob, _ = _http(u, {"User-Agent": UA, "Referer": "https://weibo.com/"}, timeout=timeout)
        except Exception:
            continue
        if not blob or len(blob) < min_size or not _is_media(blob):
            continue
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, dest)
        return True
    return False


def download_pic(url_or_id, dest, idx=None, pid=None, tries=4, timeout=120):
    """下载某张图的实况视频，失败重试。

    微博同一个实况图有多个 CDN 副本 fid（每次 status 接口随机返回其中一个，
    个别 fid 已 404），所以失败时换一份 status JSON 再试，通常第 2~3 次就命中
    可用副本。idx 为 0-based 的图片序号；pid 为 pic_id（新入库按 pid 精确匹配）。
    """
    for attempt in range(tries):
        try:
            pics = live_pics(url_or_id, nocache=attempt > 0)
        except Exception:
            time.sleep(1.5)
            continue
        cand = None
        for i, p in enumerate(pics):
            if pid and p.get("pic_id") == pid:
                cand = p
                break
            if not pid and idx is not None and i == idx:
                cand = p
                break
        if cand and cand.get("video") and download_video(cand["video"], dest, timeout=timeout):
            return True
        time.sleep(1.2)
    return False


# ── 与本地图片按内容配对 ──────────────────────────────────
def _fingerprint(src, size=16):
    """16x16 灰度指纹（PIL Image 或路径）。"""
    from PIL import Image
    try:
        im = Image.open(src) if isinstance(src, str) else src
        im = im.convert("L").resize((size, size))
        px = list(im.getdata())
    except Exception:
        return None
    avg = sum(px) / len(px) or 1.0
    return [p / avg for p in px]


def _score(a, b):
    if not a or not b:
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5 or 1.0
    db = sum(y * y for y in b) ** 0.5 or 1.0
    return num / (da * db)


def match_local_jpgs(dirpath, pics, threshold=0.90, multi=False):
    """把微博 pic 顺序对应到本地 jpg 文件（按画面内容，不依赖文件名序号）。

    返回 {pic_index(0-based): 本地绝对路径}；multi=True 时返回
    {pic_index: [同一张图的多个本地副本]}（同一图被重复下载过时全部配上）。
    """
    try:
        locals_ = [os.path.join(dirpath, f) for f in sorted(os.listdir(dirpath))
                   if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                   and not f.startswith(".") and f != "_thumb.jpg"]
    except Exception:
        return {}
    if not locals_:
        return {}
    lfps = [(p, _fingerprint(p)) for p in locals_]
    lfps = [(p, f) for p, f in lfps if f]
    pairs = []
    for i, pic in enumerate(pics):
        sp = pic.get("small") or pic.get("still")
        if not sp:
            continue
        try:
            blob, _ = _http(sp, {"User-Agent": UA, "Referer": "https://weibo.com/"}, timeout=30)
        except Exception:
            continue
        import io
        from PIL import Image
        try:
            fp = _fingerprint(Image.open(io.BytesIO(blob)))
        except Exception:
            continue
        for p, f in lfps:
            pairs.append((_score(fp, f), i, p))
    pairs.sort(reverse=True)
    used_i, used_p, out = set(), set(), {}
    DUP = max(threshold, 0.985)   # 同一张图的“副本”必须几乎一模一样，避免把视频封面误配
    for sc, i, p in pairs:
        if sc < threshold or p in used_p:
            continue
        if i in used_i:
            if not multi or sc < DUP:
                continue
        used_i.add(i)
        used_p.add(p)
        if multi:
            out.setdefault(i, []).append(p)
        else:
            out[i] = p
    return out


def local_jpgs(dirpath):
    try:
        return [os.path.join(dirpath, f) for f in sorted(os.listdir(dirpath))
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
                and not f.startswith(".") and f != "_thumb.jpg"]
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://weibo.com/3887046428/5070166123616615"
    pics = live_pics(u)
    print(f"{len(pics)} pics, live={sum(1 for p in pics if p['video'])}")
    for i, p in enumerate(pics, 1):
        print(i, p["live"], p["w"], p["h"], (p["video"] or "-")[:80])
