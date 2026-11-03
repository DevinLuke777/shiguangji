#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重下微博图片，按文件头保留动图（.gif）。
用法: python3 refetch_weibo_gif.py <item_id> [...]
旧的 <title>_N.jpg 会先移到备份目录，再重新下载。
"""
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

MEDIA = "/vol1/1000/Downloads/拾光集"
DB = "/vol1/1000/Docker/shiguangji/media_library.db"
BACKUP = "/vol1/1000/Hermes/备份/weibo-jpg-backup"
LITE = "http://127.0.0.1:8086"
PROXY = {"http": "http://127.0.0.1:2080", "https": "http://127.0.0.1:2080"}
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
OP = urllib.request.build_opener(urllib.request.ProxyHandler(PROXY))
IMG_EXT = (".jpg", ".jpeg", ".gif", ".png", ".webp")


def pid_of(url: str) -> str:
    return url.rsplit("/", 1)[-1].split(".")[0]


def fetch(url: str, ref: str = "https://weibo.com/") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
    return OP.open(req, timeout=90).read()


def parse(url: str) -> dict:
    q = LITE + "/video/share/url/parse?url=" + urllib.parse.quote(url, safe="")
    last = None
    for i in range(2):
        try:
            d = json.loads(fetch(q).decode("utf-8", "ignore"))
            if d.get("code") == 200:
                return d.get("data") or {}
        except Exception as e:
            last = e
            time.sleep(3)
    print("  解析失败:", last)
    return {}


def sniff(blob: bytes) -> str:
    if blob[:4] == b"GIF8":
        return ".gif"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def main():
    os.makedirs(BACKUP, exist_ok=True)
    con = sqlite3.connect(DB)
    for item_id in [int(x) for x in sys.argv[1:]]:
        row = con.execute("select title,original_url,local_path from items where id=?", (item_id,)).fetchone()
        if not row:
            print(f"[{item_id}] 无此条目")
            continue
        title, url, rel = row
        base = os.path.join(MEDIA, rel)
        os.makedirs(base, exist_ok=True)
        # 旧图挪去备份
        for f in os.listdir(base):
            if f.lower().endswith(IMG_EXT) and not f.startswith("_thumb"):
                dst = os.path.join(BACKUP, f"{item_id}_{f}")
                try:
                    shutil.move(os.path.join(base, f), dst)
                except Exception as e:
                    print("  移动失败", f, e)
        data = parse(url)
        images = data.get("images") or []
        print(f"[{item_id}] {title[:18]} 共 {len(images)} 张")
        for idx, im in enumerate(images, 1):
            short = im.get("url") or ""
            pid = pid_of(short) if short else ""
            cands = [short, f"https://wx1.sinaimg.cn/original/{pid}.jpg" if pid else ""]
            for cand in cands:
                if not cand:
                    continue
                try:
                    blob = fetch(cand)
                except Exception:
                    continue
                if len(blob) < 1000:
                    continue
                ext = sniff(blob)
                dst = os.path.join(base, f"{title}_{idx}{ext}")
                with open(dst, "wb") as f:
                    f.write(blob)
                os.chmod(dst, 0o644)
                print(f"    {os.path.basename(dst)} {len(blob)//1024}KB {ext}")
                break
    con.close()


if __name__ == "__main__":
    main()
