#!/usr/bin/env python3
"""
拾光集 · 缩略图生成脚本（并行版）
用法:
  python3 gen_thumbs.py          # 串行生成（兼容旧用法）
  python3 gen_thumbs.py -j 4     # 4 进程并行生成（ffmpeg CPU 密集，用进程池）
  python3 gen_thumbs.py --update-db  # 同时更新数据库 cover_path 字段
"""
import argparse
import os
import sqlite3
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from config import DB_PATH, MEDIA_ROOT, VIDEO_EXT, IMAGE_EXT
from db import get_conn, update_item
from logger import log


def gen_thumb(src_path: str, thumb_path: str, is_image: bool = False) -> bool:
    """生成缩略图：视频抽帧 / 图片直接缩放"""
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


def process_item(item_id: int, rel: str, update_db: bool = False) -> str:
    """处理单个条目，返回结果描述"""
    if not rel:
        return "skip"
    d = os.path.join(MEDIA_ROOT, rel)
    if not os.path.isdir(d):
        return "skip"
    thumb = os.path.join(d, "_thumb.jpg")
    if os.path.exists(thumb):
        return "exists"

    video = None
    img = None
    for f in os.listdir(d):
        low = f.lower()
        if video is None and low.endswith(VIDEO_EXT):
            video = os.path.join(d, f)
        if img is None and low.endswith(IMAGE_EXT) and f != "_thumb.jpg":
            img = os.path.join(d, f)

    if video:
        ok = gen_thumb(video, thumb)
    elif img:
        ok = gen_thumb(img, thumb, is_image=True)
    else:
        return "skip"

    if ok:
        # 更新数据库 cover_path
        if update_db:
            cover_rel = os.path.join(rel, "_thumb.jpg").replace("\\", "/")
            update_item(item_id, {"cover_path": cover_rel})
        return "ok"
    return "fail"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", "--jobs", type=int, default=1, help="并行线程数")
    ap.add_argument("--update-db", action="store_true", help="同时更新数据库 cover_path 字段")
    args = ap.parse_args()

    conn = get_conn()
    items = conn.execute("SELECT id, local_path FROM items").fetchall()
    conn.close()

    made, skipped, failed = 0, 0, 0

    if args.jobs <= 1:
        # 串行
        for item_id, rel in items:
            result = process_item(item_id, rel, args.update_db)
            if result == "ok":
                made += 1
                log.info(f"✅ item {item_id}")
            elif result == "exists":
                skipped += 1
            elif result == "fail":
                failed += 1
                log.warning(f"❌ item {item_id}")
    else:
        # 并行（进程池：ffmpeg 是 CPU 密集，绕过 GIL）
        # 注意：进程池模式下 update_db 在子进程里执行，用独立连接
        with ProcessPoolExecutor(max_workers=args.jobs) as executor:
            futures = {
                executor.submit(process_item, item_id, rel, args.update_db): item_id
                for item_id, rel in items
            }
            for future in as_completed(futures):
                item_id = futures[future]
                result = future.result()
                if result == "ok":
                    made += 1
                    log.info(f"✅ item {item_id}")
                elif result == "exists":
                    skipped += 1
                elif result == "fail":
                    failed += 1
                    log.warning(f"❌ item {item_id}")

    log.info(f"完成: 生成 {made} 个, 跳过 {skipped} 个已有, 失败 {failed} 个")


if __name__ == "__main__":
    main()
