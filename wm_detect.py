#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拾光集 · 水印检测（形状识别版，不依赖 OCR）

原理：小红书水印是「白底圆角药丸 + 深色『小红书』字」的固定徽标。
      在画面底部条带里找满足以下条件的近白连通块：
        · 宽高比 2.2–5.2（药丸形）
        · 尺寸随分辨率成比例（宽度约占画幅 6%–14%）
        · 内部有 6%–45% 的深色像素（字）
      多帧命中即判定带水印，避免背景白块误报。

对外函数：
  detect(path, is_video=True) -> bool
"""
import os
import subprocess
import tempfile

import numpy as np
from PIL import Image

BAND_TOP = 0.70          # 只看底部 30%
WHITE_MIN = 185          # 近白阈值
SAT_MAX = 34             # 近白要求低饱和
DARK_MAX = 120           # 字色阈值


def _frame(path, ss):
    t = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    try:
        r = subprocess.run(["ffmpeg", "-y", "-ss", str(ss), "-i", path, "-frames:v", "1",
                            "-vf", "scale=480:-2", t], capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(t) or os.path.getsize(t) == 0:
            return None
        return np.asarray(Image.open(t).convert("RGB"), dtype=np.uint8)
    except Exception:
        return None
    finally:
        if os.path.exists(t):
            try:
                os.unlink(t)
            except OSError:
                pass


def _components(mask):
    """4 邻域连通块，返回 [(x0,y0,x1,y1,area)]（mask 很小，纯 Python 够快）"""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            x0 = x1 = x
            y0 = y1 = y
            area = 0
            while stack:
                cy, cx = stack.pop()
                area += 1
                if cx < x0: x0 = cx
                if cx > x1: x1 = cx
                if cy < y0: y0 = cy
                if cy > y1: y1 = cy
                for ny, nx in ((cy-1, cx), (cy+1, cx), (cy, cx-1), (cy, cx+1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            out.append((x0, y0, x1, y1, area))
    return out


def _found_badge(img):
    """在单帧里找小红书徽标"""
    h, w, _ = img.shape
    band = img[int(h * BAND_TOP):, :, :]
    r = band[:, :, 0].astype(np.int32)
    g = band[:, :, 1].astype(np.int32)
    b = band[:, :, 2].astype(np.int32)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    white = (mn > WHITE_MIN) & ((mx - mn) < SAT_MAX)
    lum = (r * 299 + g * 587 + b * 114) // 1000
    dark = lum < DARK_MAX

    bw = band.shape[1]
    for x0, y0, x1, y1, area in _components(white):
        cw, ch = x1 - x0 + 1, y1 - y0 + 1
        if ch < 6 or cw < 24:
            continue
        ratio = cw / ch
        if not (1.55 <= ratio <= 6.0):
            continue
        if not (0.035 * bw <= cw <= 0.26 * bw):
            continue
        # 药丸填充率（圆角矩形约 0.78–0.92）
        fill = area / (cw * ch)
        if not (0.42 <= fill <= 0.98):
            continue
        # 位置：徽标在右 30%（或左侧 ID 区）
        if not (x0 > 0.68 * bw or x1 < 0.32 * bw):
            continue
        # 内部要有字
        inner = dark[y0:y1 + 1, x0:x1 + 1]
        if inner.size == 0:
            continue
        dark_ratio = inner.mean()
        if not (0.04 <= dark_ratio <= 0.55):
            continue
        return True
    return False


def _duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def detect(path: str, is_video: bool = True, frames: int = 5) -> bool:
    if not path or not os.path.exists(path):
        return False
    if not is_video:
        img = _frame(path, 0)
        return bool(img is not None and _found_badge(img))

    dur = _duration(path) or 30.0
    hits = 0
    for i in range(frames):
        ss = max(0.5, dur * (0.15 + 0.25 * i))
        img = _frame(path, round(ss, 2))
        if img is None:
            continue
        if _found_badge(img):
            hits += 1
    return hits >= 2   # 多帧中命中 2 帧即判定


if __name__ == "__main__":
    import sys
    p = sys.argv[1]
    print("带水印" if detect(p, p.lower().endswith((".mp4", ".mov", ".m4v"))) else "未检测到")
