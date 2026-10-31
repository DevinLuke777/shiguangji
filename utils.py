#!/usr/bin/env python3
"""
拾光集 · 公共工具函数
"""
import os
import re
import time
import urllib.request
import urllib.parse
from collections import OrderedDict
from typing import Optional, Tuple, List
from config import UA_MOBILE, UA_DESKTOP, VIDEO_EXT, IMAGE_EXT, MEDIA_ROOT
from logger import log

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
    """清洗标题：去首尾非法字符、换行、话题标签、截断"""
    t = (t or "").strip()
    while t and not re.match(r"[一-鿿A-Za-z0-9]", t[0]):
        t = t[1:]
    t = re.sub(r'[\n\r]+', ' ', t).strip()
    t = re.sub(r'(\s*#\S+\s*)+$', '', t).strip()
    if len(t) > 80:
        t = t[:80].rstrip()
    return t


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """过滤文件名非法字符"""
    return "".join(c for c in name if c not in ':：/\\*?"<>|')[:max_len]


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
        subprocess.run([
            "exiftool", "-overwrite_original", "-q",
            f"-MakerApple65={pair_uuid}",
            "-Make=Apple",
            jpg_path
        ], capture_output=True, timeout=10)
    except Exception as e:
        log.warning(f"JPG Live Photo 元数据写入失败 {jpg_path}: {e}")

    # MOV 侧：写 ContentIdentifier + StillImageTime
    # StillImageTime 需要写入 metadata track，exiftool 用 -api 支持有限
    # 实测：ContentIdentifier 足以让 iPhone 识别配对
    try:
        subprocess.run([
            "exiftool", "-overwrite_original", "-q",
            f"-ContentIdentifier={pair_uuid}",
            video_path
        ], capture_output=True, timeout=10)
        log.info(f"Live Photo 元数据注入完成: {basename} (UUID={pair_uuid[:8]}...)")
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
