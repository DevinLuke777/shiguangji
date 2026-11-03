#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回填 items.content_key（平台原生 ID）——短链也能补上，可重复跑。

DB 里 90% 的 original_url 是短链（v.douyin.com / xhslink.com），直接正则取不到 ID，
这里跟一次重定向拿真实链接再取。结果缓存到 .tmp/ck_cache.json，中断可续。

用法:
  python3 backfill_content_key.py --dry-run
  python3 backfill_content_key.py --workers 16
"""
import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import content_key_from_url  # noqa: E402

DB = os.environ.get("DB_PATH", "/vol1/1000/Docker/shiguangji/media_library.db")
CACHE = os.environ.get("CK_CACHE", "/vol1/1000/Docker/shiguangji/.tmp/ck_cache.json")


def load_cache() -> dict:
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cache = load_cache()
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    if "content_key" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN content_key TEXT DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS idx_content_key ON items(content_key)")
        con.commit()

    rows = [dict(r) for r in con.execute(
        "SELECT id, original_url, content_key FROM items ORDER BY id")]
    todo = [r for r in rows if (r["original_url"] or "") and not (r["content_key"] or "")]
    if args.limit:
        todo = todo[:args.limit]
    print(f"待补 {len(todo)} 条（已有 {sum(1 for r in rows if r['content_key'])}/{len(rows)}）")
    if args.dry_run or not todo:
        for r in todo[:8]:
            print("  样例:", r["id"], r["original_url"][:70])
        return 0

    def work(r):
        u = r["original_url"]
        ck = cache.get(u)
        if ck is None:
            ck = content_key_from_url(u, resolve=True)
            cache[u] = ck
        return r["id"], ck

    done = hit = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for iid, ck in ex.map(work, todo):
            done += 1
            if ck:
                con.execute("UPDATE items SET content_key=? WHERE id=?", (ck, iid))
                hit += 1
            if done % 200 == 0:
                con.commit()
                print(f"  {done}/{len(todo)} 命中 {hit}", flush=True)
    con.commit()
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    total = con.execute("SELECT count(*) FROM items WHERE content_key!=''").fetchone()[0]
    print(f"完成：本次补 {hit} 条，全库已有 content_key {total}/{len(rows)}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
