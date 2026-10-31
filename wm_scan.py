#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拾光集 · 水印自检

两种判定：
  1) 来源标签判定（无依赖）：src_tag 309/258/orig/ok → ok；259/render/wm → suspect
  2) OCR 判定（--ocr，需要 tesseract + chi_sim）：抽帧/读图 → 裁下 30% → 识别「小红书」等字样

用法：
  python3 wm_scan.py --limit 20            # 只按来源标签判定 20 条未检测的
  python3 wm_scan.py --ocr --limit 20      # 加 OCR 复核（会慢一些）
  python3 wm_scan.py --recheck --limit 50  # 重新检查已标 suspect 的
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

CLEAN_TAGS = {"309", "258", "orig", "ok"}
DIRTY_TAGS = {"259", "render", "wm"}

OCR_HINTS = ("小红书", "xiaohongshu", "XHS", "小红书号")
try:
    from wm_detect import detect as shape_detect
except Exception:          # 缺 numpy 等依赖时降级
    shape_detect = None


def pick_rows(limit: int, recheck: bool):
    import db
    conn = db.get_conn()
    try:
        where = "wm_flag='suspect'" if recheck else "(wm_flag='' OR wm_flag IS NULL)"
        rows = conn.execute(
            f"SELECT id, platform, title, local_path, media_type, src_tag, wm_flag "
            f"FROM items WHERE {where} ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_flag(item_id: int, flag: str):
    import db
    conn = db.get_conn()
    try:
        conn.execute("UPDATE items SET wm_flag=? WHERE id=?", (flag, item_id))
        conn.commit()
    finally:
        conn.close()


def first_media(rel: str, media_type: str = ""):
    """返回 (文件路径, 是否视频)。按目录实际内容判断，别信 DB 里的 media_type。"""
    d = os.path.join(os.environ.get("MEDIA_ROOT", ""), rel or "")
    if not os.path.isdir(d):
        return None
    vids = [f for f in os.listdir(d)
            if f.lower().endswith((".mp4", ".mov", ".m4v", ".webm")) and not f.startswith(".")]
    if vids:
        vids.sort()
        return os.path.join(d, vids[0]), True
    imgs = [f for f in os.listdir(d)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            and "_thumb" not in f.lower() and not f.startswith(".")]
    if imgs:
        imgs.sort()
        return os.path.join(d, imgs[0]), False
    return None


def ocr_watermark(path: str, is_video: bool) -> bool:
    """用 ffmpeg 裁底部 32%（放大 2 倍）+ tesseract 识别，返回是否命中水印字样"""
    png = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    try:
        crop = "crop=iw:ih*0.32:0:ih*0.68,scale=iw*2:-2"
        if is_video:
            cmd = ["ffmpeg", "-y", "-ss", "2", "-i", path, "-frames:v", "1", "-vf", crop, png]
            subprocess.run(cmd, capture_output=True, timeout=90)
            if not os.path.exists(png) or os.path.getsize(png) == 0:
                # 视频太短时从第 0 秒抽
                cmd = ["ffmpeg", "-y", "-i", path, "-frames:v", "1", "-vf", crop, png]
                subprocess.run(cmd, capture_output=True, timeout=90)
        else:
            cmd = ["ffmpeg", "-y", "-i", path, "-frames:v", "1", "-vf", crop, png]
            subprocess.run(cmd, capture_output=True, timeout=90)
        if not os.path.exists(png) or os.path.getsize(png) == 0:
            return False
        out = subprocess.run(["tesseract", png, "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                             capture_output=True, text=True, timeout=120)
        text = (out.stdout or "").replace(" ", "").replace("\n", "")
        return any(k.replace(" ", "") in text for k in OCR_HINTS)
    except Exception:
        return False
    finally:
        if os.path.exists(png):
            try:
                os.unlink(png)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--ocr", action="store_true", help="启用 OCR 复核")
    ap.add_argument("--recheck", action="store_true", help="重查已标 suspect 的")
    args = ap.parse_args()

    rows = pick_rows(args.limit, args.recheck)
    if not rows:
        print("没有待检测的条目")
        return

    has_tess = shutil.which("tesseract") is not None
    stat = {"ok": 0, "suspect": 0, "skip": 0}
    for it in rows:
        tag = (it.get("src_tag") or "").strip()
        flag = ""
        if tag in CLEAN_TAGS:
            flag = "ok"
        elif tag in DIRTY_TAGS:
            flag = "suspect"

        # 老数据没有来源标签：用形状识别判断（命中才标 suspect，没命中留空不冤枉）
        if flag == "" and shape_detect is not None:
            media = first_media(it["local_path"], it.get("media_type") or "")
            if media:
                f, is_vid = media
                if shape_detect(f, is_vid):
                    flag = "suspect"
            else:
                stat["nofile"] = stat.get("nofile", 0) + 1

        if not flag:
            stat["skip"] += 1
            continue
        set_flag(it["id"], flag)
        stat[flag] += 1
        print(f"[{flag}] #{it['id']} {it['platform']} {it['title'][:28]} (src_tag={tag or '-'})")

    print(f"\n完成：ok {stat['ok']} · suspect {stat['suspect']} · 无法判定 {stat['skip']}"
          + ("" if has_tess else "（未装 tesseract，OCR 未启用）"))


if __name__ == "__main__":
    main()
