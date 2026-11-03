#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 items.cover_path，并在缺 _thumb.jpg 时补生成。
- 图片：PIL 缩到宽 720 存 _thumb.jpg
- 视频：ffmpeg 取第 1 秒一帧
用法: python3 fix_covers.py [--dry-run]
"""
import os
import sqlite3
import subprocess
import sys

DB = os.environ.get("DB_PATH", "/vol1/1000/Docker/shiguangji/media_library.db")
MEDIA = os.environ.get("MEDIA_ROOT", "/vol1/1000/Downloads/拾光集")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
VID_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
THUMB = "_thumb.jpg"


def make_thumb(full: str) -> bool:
    dst = os.path.join(full, THUMB)
    if os.path.isfile(dst) and os.path.getsize(dst) > 1000:
        return True
    try:
        names = os.listdir(full)
    except Exception:
        return False
    vids = [f for f in sorted(names) if f.lower().endswith(VID_EXT)]
    if vids:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i",
               os.path.join(full, vids[0]), "-frames:v", "1",
               "-vf", "scale=720:-2", dst]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception:
            r = None
        if r is not None and r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 1000:
            os.chmod(dst, 0o644)
            return True
    imgs = [f for f in sorted(names) if f.lower().endswith(IMG_EXT)]
    if imgs:
        try:
            from PIL import Image
            im = Image.open(os.path.join(full, imgs[0])).convert("RGB")
            w, h = im.size
            if w > 720:
                im = im.resize((720, int(h * 720 / w)), Image.LANCZOS)
            im.save(dst, "JPEG", quality=86)
            os.chmod(dst, 0o644)
            return True
        except Exception:
            return False
    return False


def resolve_dir(rel: str) -> str:
    """目录不存在时，按前缀找同名变体（下载器重名会加 _2、或带话题后缀）。"""
    full = os.path.join(MEDIA, rel)
    if os.path.isdir(full):
        return full
    parent = os.path.dirname(full)
    base = os.path.basename(full)
    try:
        cands = [os.path.join(parent, d) for d in os.listdir(parent)
                 if d.startswith(base) and os.path.isdir(os.path.join(parent, d))]
    except Exception:
        return full
    cands = [c for c in cands if os.path.isfile(os.path.join(c, THUMB)) or
             any(f.lower().endswith(IMG_EXT + VID_EXT) for f in os.listdir(c))]
    if not cands:
        return full
    return max(cands, key=os.path.getmtime)


def cover_for(rel: str, make: bool) -> str:
    full = resolve_dir(rel)
    if not os.path.isdir(full):
        return ""
    real_rel = os.path.relpath(full, MEDIA).replace("\\", "/")
    if make and not os.path.isfile(os.path.join(full, THUMB)):
        make_thumb(full)
    if os.path.isfile(os.path.join(full, THUMB)):
        return os.path.join(real_rel, THUMB)
    try:
        for f in sorted(os.listdir(full)):
            if f.lower().endswith(IMG_EXT):
                return os.path.join(real_rel, f)
    except Exception:
        return ""
    return ""


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT id, local_path FROM items "
        "WHERE cover_path IS NULL OR cover_path='' ORDER BY id DESC").fetchall()
    fixed = 0
    for item_id, rel in rows:
        full = resolve_dir(rel or "")
        real_rel = os.path.relpath(full, MEDIA).replace("\\", "/") if os.path.isdir(full) else (rel or "")
        c = cover_for(rel or "", not dry)
        if not c:
            print(f"MISS {item_id} {rel}（目录缺失或无媒体文件）")
            continue
        print(("DRY " if dry else "FIX ") + f"{item_id} {c}")
        if not dry:
            cur.execute("UPDATE items SET cover_path=? WHERE id=?", (c, item_id))
            if real_rel and real_rel != rel:
                cur.execute("UPDATE items SET local_path=? WHERE id=?", (real_rel, item_id))
                print(f"    路径修正: {rel} -> {real_rel}")
        fixed += 1
    if not dry:
        con.commit()
    print(f"共 {len(rows)} 条空封面，处理 {fixed} 条")
    con.close()


if __name__ == "__main__":
    main()
