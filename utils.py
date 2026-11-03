#!/usr/bin/env python3
"""
拾光集 · 公共工具函数
"""
import os
import re
import time
import hashlib
import urllib.request
import urllib.parse
from collections import OrderedDict
from typing import Optional, Tuple, List
from config import UA_MOBILE, UA_DESKTOP, VIDEO_EXT, IMAGE_EXT, MEDIA_ROOT
from logger import log

# ── 内容指纹 / 回收目录（去重规则）──────────────────
# v2（2026-11-01）：确定性算法 —— 目录内**所有**媒体文件，按文件名排序，逐个 update
#   文件名 + 大小 + 前 1MB 内容，最后拼上文件总数。视频/图文/实况同一条路径。
#   v1 的「挑最大视频只读前 8MB / 有视频就不看图片」在实况图（jpeg+同名 mp4 混放）上
#   会挑错文件，同一内容算出不同指纹，去重完全失效 —— 不要再改回这种分支。
FP_VER = "v2"
FP_HEAD = 1024 * 1024             # 每文件只读前 1MB（全量哈希 4000 个目录太慢）
TRASH_DUP = os.environ.get("TRASH_DUP_DIR", "/vol1/1000/Downloads/_trash_dup")

# 全局 opener（不走代理，本机服务防劫持）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url: str, timeout: int = 20, referer: Optional[str] = None,
          extra_headers: Optional[dict] = None, use_proxy: bool = False) -> bytes:
    """
    统一 HTTP 请求函数。
    默认不走代理（本机服务防劫持），X/Twitter 等海外平台传 use_proxy=True。
    """
    headers = {"User-Agent": UA_MOBILE}
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers)

    if use_proxy:
        from config import PROXY_URL
        proxy_handler = urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
        opener = urllib.request.build_opener(proxy_handler)
        opener.addheaders = [("User-Agent", UA_DESKTOP)]
        return opener.open(req, timeout=timeout).read()

    return _OPENER.open(req, timeout=timeout).read()


def clean_title(t: str) -> str:
    """清洗标题：去首尾非法字符、换行、话题标签、截断

    ⚠️ 纯 emoji 标题（🍸 / ⛵️ / _(:з」∠)_）不能洗成空串 —— 空串会让
    目录名变空、落盘直接进「平台/日期/」目录，入库就成没有媒体的假条目
    （2026-11-01 用户报「主页展开的条目没视频」根因之一）。洗空了就退回原文。
    """
    orig = (t or "").strip()
    t = orig
    while t and not re.match(r"[一-鿿A-Za-z0-9]", t[0]):
        t = t[1:]
    if not t.strip():
        t = orig                      # 整条都是 emoji/符号 -> 保留原文
    t = re.sub(r'[\n\r\t]+', ' ', t).strip()
    t = t.replace("/", "／").replace("\\", "＼")
    t = re.sub(r'(\s*#\S+\s*)+$', '', t).strip()
    if not t.strip():
        t = orig
    if len(t) > 80:
        t = t[:80].rstrip()
    return t


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """过滤文件名非法字符

    同时去掉前导点：目录/文件以 "." 开头会被当成隐藏文件，
    页面（list_item_files 跳过 . 开头）就看不到 → 表现为「有标题没视频」。
    """
    s = "".join(c for c in name if c not in ':：/\\*?"<>|')
    s = s.strip().lstrip(".").strip()
    return s[:max_len]


def is_video(filename: str) -> bool:
    """判断是否为视频文件"""
    return filename.lower().endswith(VIDEO_EXT)


def is_image(filename: str) -> bool:
    """判断是否为图片文件"""
    return filename.lower().endswith(IMAGE_EXT)


def list_item_files(rel_path: str) -> List[str]:
    """
    列出条目目录下的媒体文件（相对路径），供详情页使用。
    排除隐藏文件和 _thumb.jpg，按自然排序。
    返回 [rel_path/f1, rel_path/f2, ...]
    """
    if not rel_path:
        return []
    try:
        d = safe_media_path(rel_path)
    except ValueError:
        return []
    if not os.path.isdir(d):
        return []

    def natural_key(s: str):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

    files = []
    for f in sorted(os.listdir(d), key=natural_key):
        if f.startswith(".") or f == "_thumb.jpg":
            continue
        files.append(os.path.join(rel_path, f).replace("\\", "/"))
    return files


def safe_media_path(rel_path: str) -> str:
    """
    安全拼接媒体路径，防路径穿越。
    返回绝对路径，如果越界则抛 ValueError。
    """
    from pathlib import Path
    full = (Path(MEDIA_ROOT) / rel_path).resolve()
    full.relative_to(Path(MEDIA_ROOT).resolve())  # 越界抛 ValueError
    return str(full)


def pick_stream(html: str) -> Tuple[Optional[str], Optional[str]]:
    """
    小红书视频流选择：优先 309(X265) → 258(X264) → 兜底 259(带水印)。
    返回 (url, tag)。
    """
    for tag in ("309", "258"):
        m = re.search(r'"masterUrl":"([^"]+_%s\.mp4[^"]*)"' % tag, html)
        if m:
            return m.group(1).replace("\\u002F", "/").replace("\\/", "/"), tag
    m = re.search(r'"masterUrl":"([^"]+)"', html)
    if not m:
        return None, None
    return m.group(1).replace("\\u002F", "/").replace("\\/", "/"), "259"


def platform_label(platform: str) -> str:
    """平台显示名称"""
    return "推特" if platform == "X" else platform


def parse_xhs_meta(html: str) -> dict:
    """
    从小红书页面 HTML 提取元数据（worker + ingest 共用）。
    优先级：<title> > noteDetailMap JSON > type JSON。
    返回 {title, author_name, author_avatar, content, post_time, has_video}
    """
    # 标题：优先 <title>（权威「笔记标题 - 小红书」），再 note JSON
    title = None
    mt = re.search(r'<title>([^<]*?)</title>', html)
    if mt:
        t = mt.group(1).replace(" - 小红书", "").strip()
        if t and t != "小红书":
            title = t
    if not title:
        mn = re.search(r'"title":"([^"]*)"', html)
        if mn and mn.group(1) and "想了解" not in mn.group(1):
            title = mn.group(1)

    # 作者
    author = "小红书用户"
    ma = re.search(r'"nickname":"([^"]*)"', html)
    if not ma:
        ma = re.search(r'"nickName":"([^"]+)"', html)
    if ma:
        author = ma.group(1)

    # 头像
    avatar = None
    av_m = re.search(r'"avatar":"([^"]+)"', html)
    if av_m:
        avatar = av_m.group(1).replace("\\u002F", "/").replace("\u002F", "/")

    # 正文
    content = None
    d = re.search(r'"desc":"([^"]*)"', html)
    if d:
        content = d.group(1)[:500]

    # 发布时间
    post_time = None
    tm = re.search(r'"time":(\d{10})', html)
    if tm:
        from datetime import datetime
        post_time = datetime.fromtimestamp(int(tm.group(1))).strftime("%Y-%m-%d %H:%M:%S")

    # 是否含视频
    has_video = bool(re.search(r'"masterUrl":"[^"]+_(259|309|258)\.mp4', html))

    return {
        "title": title,
        "author_name": author,
        "author_avatar": avatar,
        "content": content,
        "post_time": post_time,
        "has_video": has_video,
    }


def gen_thumb_ffmpeg(src_path: str, thumb_path: str, is_image: bool = False) -> bool:
    """
    用 ffmpeg 生成缩略图。
    视频抽帧 / 图片缩放，统一入口。
    """
    import subprocess
    try:
        cmd = ["ffmpeg", "-y"]
        if not is_image:
            cmd += ["-ss", "0.5"]
        cmd += ["-i", src_path, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", thumb_path]
        subprocess.run(cmd, capture_output=True, timeout=60)
        return os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 3000
    except Exception as e:
        log.warning(f"缩略图生成失败 {src_path}: {e}")
        return False


def _check_tool(name: str) -> bool:
    """检查命令行工具是否可用"""
    import subprocess
    try:
        subprocess.run([name, "-version"] if name != "exiftool" else [name, "-ver"],
                       capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def make_live_photo(jpg_path: str, video_path: str) -> str:
    """
    把「静态图 + 短视频」对转换成 iPhone Live Photo 格式。

    iPhone Live Photo 要求：
    1. JPG 和 MOV 同名（如 live_1.jpg + live_1.mov）
    2. JPG 的 EXIF 里有 MakerApple65 (MediaGroupUUID)
    3. MOV 有 com.apple.quicktime.contentidentifier（和 JPG 的 UUID 一致）
    4. MOV 有 com.apple.quicktime.still-image-time（标记静态帧时间点）

    需要 ffmpeg（mp4→mov 转换）+ exiftool（注入元数据）。
    如果 exiftool 不可用，降级为只保证同名（用户可用 iOS 快捷指令配对）。

    返回最终的 mov 路径（可能是转换后的）。
    """
    import subprocess
    import uuid as _uuid

    if not os.path.isfile(jpg_path) or not os.path.isfile(video_path):
        return video_path

    basename = os.path.splitext(os.path.basename(jpg_path))[0]  # 如 live_1
    video_dir = os.path.dirname(video_path)
    mov_target = os.path.join(video_dir, f"{basename}.mov")

    # ── 1. mp4 → mov（H.264 copy，不重新编码）──
    if not video_path.lower().endswith(".mov"):
        try:
            cmd = ["ffmpeg", "-y", "-i", video_path, "-c", "copy", "-f", "mov", mov_target]
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if r.returncode == 0 and os.path.isfile(mov_target) and os.path.getsize(mov_target) > 1000:
                # 删除原 mp4
                if video_path != mov_target:
                    try:
                        os.remove(video_path)
                    except Exception:
                        pass
                video_path = mov_target
            else:
                # 转换失败，用原文件
                if os.path.isfile(mov_target):
                    try:
                        os.remove(mov_target)
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"mp4→mov 转换失败 {video_path}: {e}")
    elif video_path != mov_target:
        # 已是 mov 但名字不对，改名
        try:
            os.rename(video_path, mov_target)
            video_path = mov_target
        except Exception:
            pass

    # ── 2. exiftool 注入 Apple Live Photo 元数据 ──
    if not _check_tool("exiftool"):
        log.info(f"exiftool 不可用，Live Photo 元数据未注入（JPG+MOV 已同名配对）")
        return video_path

    # 生成配对 UUID（大写，去横线）
    pair_uuid = _uuid.uuid4().hex.upper()

    # JPG 侧：写 MakerApple65 (MediaGroupUUID) + Make=Apple
    try:
        r = subprocess.run([
            "exiftool", "-overwrite_original",
            f"-MakerApple65={pair_uuid}",
            "-Make=Apple",
            jpg_path
        ], capture_output=True, timeout=10, text=True)
        if "1 image files updated" not in (r.stdout or "") + (r.stderr or ""):
            # 多数 exiftool 版本不允许凭空创建 MakerApple 块，图片侧元数据写不进是已知限制
            log.info(f"JPG 侧 Apple 元数据未写入（exiftool 不支持创建 MakerApple 块）: {basename}")
    except Exception as e:
        log.warning(f"JPG Live Photo 元数据写入失败 {jpg_path}: {e}")

    # MOV 侧：写 ContentIdentifier（必须用 Keys: 前缀，否则 exiftool 会「unchanged」静默失败）
    # StillImageTime 需要写 metadata track，exiftool 支持有限；ContentIdentifier 足以让 iPhone 识别配对
    try:
        r = subprocess.run([
            "exiftool", "-overwrite_original",
            f"-Keys:ContentIdentifier={pair_uuid}",
            video_path
        ], capture_output=True, timeout=10, text=True)
        if "1 image files updated" in (r.stdout or "") + (r.stderr or ""):
            log.info(f"Live Photo 元数据注入完成: {basename} (UUID={pair_uuid[:8]}...)")
        else:
            log.warning(f"MOV ContentIdentifier 未写入: {(r.stderr or r.stdout or '').strip()[:120]}")
    except Exception as e:
        log.warning(f"MOV Live Photo 元数据写入失败 {video_path}: {e}")

    return video_path


class AvatarCache:
    """
    头像缓存：LRU + TTL。
    - 最大容量 max_size，超出淘汰最旧
    - 每条 ttl 秒过期，过期后重新拉取
    """
    def __init__(self, max_size: int = 200, ttl: int = 3600):
        self._store: OrderedDict = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl

    def get(self, url: str) -> Optional[bytes]:
        if url not in self._store:
            return None
        data, ts = self._store[url]
        if time.time() - ts > self._ttl:
            # 过期，移除
            del self._store[url]
            return None
        # 命中，移到末尾（LRU）
        self._store.move_to_end(url)
        return data

    def set(self, url: str, data: bytes) -> None:
        self._store[url] = (data, time.time())
        self._store.move_to_end(url)
        # 淘汰超出容量的
        while len(self._store) > self._max_size:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()


def _sha256_file(path: str, head: int = 0) -> str:
    """算文件 sha256；head>0 时只读前 head 字节（视频用，省 IO）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if head:
            h.update(f.read(head))
        else:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def media_fingerprint(full_dir: str) -> str:
    """
    目录内容指纹（去重用，不看 URL，只看实际文件内容）。

    确定性算法（同一内容任何时候都算出同一个值）：
      - 取目录里**所有**媒体文件（视频+图片），排除 `_thumb*` 与 `.`/`_` 开头的辅助文件
      - **按文件名排序**（不按大小、不分类、不为「有视频」另开分支）
      - 逐个 update(文件名 + 文件大小(8字节) + 前 FP_HEAD 字节内容)
      - 最后拼上媒体文件总数
    图文 / 实况图（jpeg + 同名 mp4 混放）/ 纯视频 / 纯图片 全部走同一条路径。

    返回 "v2:<文件数>:<sha256>"；返回 "" 表示目录不存在 / 读不到 / 没有媒体文件
    （此时放弃去重判断，宁可漏杀不误杀 —— 抖音目录常是 lite.parser 700 权限）。
    """
    if not full_dir or not os.path.isdir(full_dir):
        return ""
    try:
        names = sorted(n for n in os.listdir(full_dir)
                       if not n.startswith((".", "_")))
    except (PermissionError, OSError):
        return ""
    media = [n for n in names if n.lower().endswith(VIDEO_EXT + IMAGE_EXT)]
    if not media:
        return ""
    h = hashlib.sha256()
    try:
        for n in media:
            p = os.path.join(full_dir, n)
            if not os.path.isfile(p):
                continue
            size = os.path.getsize(p)
            h.update(n.encode("utf-8", "ignore"))
            h.update(b"\0")
            h.update(size.to_bytes(8, "big"))
            with open(p, "rb") as f:
                h.update(f.read(FP_HEAD))
    except (PermissionError, OSError):
        return ""
    return f"{FP_VER}:{len(media)}:{h.hexdigest()}"


# ── 平台原生内容 ID（content_key，首选去重键）──────────────
# 换短链 / 换 xsec_token / 重新分享都改不了它，比文件指纹更稳。
_CK_RULES = (
    ("xhs", re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{24})", re.I)),
    ("dy",  re.compile(r"douyin\.com/(?:video|note|share/video)/([A-Za-z0-9]{6,})", re.I)),
    ("wb",  re.compile(r"weibo\.(?:com|cn)/(?:\d+|detail)/([A-Za-z0-9]{6,})", re.I)),
    ("bl",  re.compile(r"bilibili\.com/video/(BV[A-Za-z0-9]{10}|av\d+)", re.I)),
    ("tt",  re.compile(r"tiktok\.com/@[^/]+/(?:video|photo)/(\d+)", re.I)),
    ("x",   re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d+)", re.I)),
)
_SHORT_HOSTS = ("v.douyin.com", "xhslink.com", "xhslink.cn", "b23.tv", "t.cn", "vm.tiktok.com")


def resolve_short_url(url: str, timeout: int = 8) -> str:
    """短链跟重定向拿到真实 URL；失败原样返回（不阻塞入库）。"""
    if not url or not any(h in url for h in _SHORT_HOSTS):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA_MOBILE})
        with _OPENER.open(req, timeout=timeout) as r:
            return r.geturl() or url
    except urllib.error.HTTPError as e:
        # 跳转后的落地页 404/403 很常见，重定向目标在 e.url 里，别当失败丢掉
        return e.geturl() or url
    except Exception as e:
        log.warning(f"短链解析失败（用原链接算 content_key）: {e}")
        return url


def content_key_from_url(url: str, resolve: bool = True) -> str:
    """
    从链接提取平台原生 ID → "xhs:6ac1d414000000001500ee79" 这种形式。
    短链（v.douyin.com / xhslink.com / b23.tv …）先跟重定向再取。
    取不到返回 ""（此时只用文件指纹兜底）。
    """
    if not url or url.startswith("scan://"):
        return ""
    if url.startswith("//"):          # 协议相对链接（worker 抓到的分享链接常见）
        url = "https:" + url
    cands = [url]
    r = resolve_short_url(url) if resolve else ""
    if r and r != url:
        cands += [r, urllib.parse.unquote(r)]  # 小红书短链常落到 /404/sec?originalUrl=<编码后的真链>
    for u in cands:
        if not u:
            continue
        for tag, rx in _CK_RULES:
            m = rx.search(u)
            if m:
                return f"{tag}:{m.group(1).lower() if tag in ('xhs', 'bl') else m.group(1)}"
    return ""


def trash_duplicate(full_dir: str, platform: str = "", tag: str = "") -> str:
    """把重复/失败的媒体目录移到回收目录（绝不 rm）。返回新路径，失败返回 ""。"""
    if not os.path.isdir(full_dir):
        return ""
    dst_root = os.path.join(TRASH_DUP, platform or "未知")
    try:
        os.makedirs(dst_root, exist_ok=True)
    except OSError:
        return ""
    base = os.path.basename(full_dir.rstrip("/")) or f"item_{int(time.time())}"
    if tag:
        base = f"{base}__{tag}"
    dst = os.path.join(dst_root, base)
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(dst_root, f"{base}_{n}")
        n += 1
    try:
        os.rename(full_dir, dst)
    except OSError:
        try:
            import shutil
            shutil.move(full_dir, dst)
        except Exception:
            return ""
    return dst


def download_avatar_local(avatar_url: str, dest_path: str) -> bool:
    """
    下载头像到本地文件（入库时调用，让 avatar 路由命中本地缓存）。
    根据域名设 Referer，失败返回 False 不阻塞入库。
    """
    if not avatar_url:
        return False
    # 已存在且非空就跳过
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 100:
        return True

    referer = "https://weibo.com/"
    if "xhscdn" in avatar_url:
        referer = "https://www.xiaohongshu.com/"
    elif "douyinpic" in avatar_url or "douyin" in avatar_url:
        referer = "https://www.douyin.com/"
    elif "twimg" in avatar_url:
        referer = "https://x.com/"
    elif "tiktokcdn" in avatar_url:
        referer = "https://www.tiktok.com/"

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    for ua in (UA_MOBILE, "Mozilla/5.0"):
        try:
            req = urllib.request.Request(avatar_url, headers={
                "User-Agent": ua,
                "Referer": referer,
            })
            data = _OPENER.open(req, timeout=10).read()
            if len(data) > 100 and len(data) < 2000000:
                with open(dest_path, "wb") as f:
                    f.write(data)
                return True
        except Exception:
            continue
    return False
