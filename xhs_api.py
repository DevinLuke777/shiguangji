#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书官方签名接口下载器（图文原图 + 实况 Live 图 + 视频）

用法（必须用 xhs-downloader 的 venv python 执行）：
    <venv>/bin/python xhs_api.py "<笔记URL>" <输出目录>

成功：把下载到的文件移进 <输出目录>，最后一行打印 JSON：
    {"ok": true, "files": ["xxx.png", ...], "live": ["xxx_live1.mp4", ...]}
失败：打印 {"ok": false, "msg": "..."} 并退出非 0
"""
import asyncio
import glob
import json
import os
import shutil
import sys
import tempfile

REPO = os.environ.get("XHS_DIR") or os.path.expanduser("~/.hermes/workspace/xhs-downloader")
sys.path.insert(0, REPO)
os.environ.pop("PYTHONPATH", None)

from source import XHS  # noqa: E402

COOKIE_FILE = os.environ.get("XHS_COOKIE_FILE", "")


async def _run(url, work):
    cookie = ""
    if COOKIE_FILE and os.path.isfile(COOKIE_FILE):
        cookie = open(COOKIE_FILE, encoding="utf-8").read().strip()
    async with XHS(
        work_path=work,
        folder_name="Download",
        cookie=cookie,
        folder_mode=False,
        author_archive=False,
        download_record=False,
        record_data=False,
        note_format="",
        image_download=True,
        video_download=True,
        video_cover_download=False,
        live_download=True,          # 关键：实况图
        write_mtime=False,
        timeout=20,
        max_retry=3,
    ) as xhs:
        return await xhs.extract(url, download=True)


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "msg": "用法: xhs_api.py <url> <outdir>"}))
        return 2
    url, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    work = tempfile.mkdtemp(prefix="xhs_api_")
    try:
        data = asyncio.run(_run(url, work))
        src_files = [p for p in glob.glob(os.path.join(work, "Download", "**", "*"), recursive=True)
                     if os.path.isfile(p) and not p.endswith(".db")]
        if not src_files:
            print(json.dumps({"ok": False, "msg": f"没有下载到文件 data={str(data)[:120]}"}, ensure_ascii=False))
            return 3
        files, lives = [], []
        for p in src_files:
            name = os.path.basename(p)
            dst = os.path.join(outdir, name)
            shutil.move(p, dst)
            (lives if "_live" in name.lower() else files).append(name)
        print(json.dumps({"ok": True, "files": files, "live": lives}, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "msg": f"{type(e).__name__}: {str(e)[:160]}"}, ensure_ascii=False))
        return 4
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
