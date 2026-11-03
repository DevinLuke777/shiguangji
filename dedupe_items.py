#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拾光集 · 历史去重（一次性脚本，可重复跑，幂等）

三个去重键合并判断（并查集，任一命中即同一内容）：
  1. content_key —— 平台原生 ID（小红书 note id / 抖音 aweme_id / 微博 mid …）
     换短链、换 xsec_token、重新分享都改不了它。**首选**。
  2. fingerprint —— 目录内容指纹（v2 确定性算法，见 utils.media_fingerprint）
     同一批文件被下到两个不同日期目录时靠它兜底。
  3. local_path —— 同一个目录被多条 DB 行引用（标题撞车、重复入库）时，
     磁盘上只有一份文件，多余的行必为重复。

同一组只保留 id 最小的一条；其余：目录移到回收目录（**绝不 rm**）+ 删除 DB 行。
目录还被别的条目/保留条目引用 → 只删行不动文件。搬不动的行不删（宁可留重复不丢内容）。

用法:
  python3 dedupe_items.py --dry-run         # 只看：多少组、准备删多少条、预计释放多少
  python3 dedupe_items.py                   # 实际执行（先备份 DB！）
  python3 dedupe_items.py --platform 抖音
  python3 dedupe_items.py --resolve-short   # 顺带解析 v.douyin/xhslink 短链补 content_key
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

DB = os.environ.get("DB_PATH", "/vol1/1000/Docker/shiguangji/media_library.db")
MEDIA = os.environ.get("MEDIA_ROOT", "/vol1/1000/Downloads/拾光集")
TRASH = os.environ.get("TRASH_DUP_DIR", "/vol1/1000/Downloads/_trash_dup")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import media_fingerprint, trash_duplicate, content_key_from_url  # noqa: E402


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class DSU:
    def __init__(self, ids):
        self.p = {i: i for i in ids}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if ra < rb:
                self.p[rb] = ra
            else:
                self.p[ra] = rb


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--platform", default="")
    ap.add_argument("--report", default="")
    ap.add_argument("--resolve-short", action="store_true",
                    help="解析 v.douyin.com/xhslink 短链补 content_key（慢，默认关）")
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    for col, idx in (("fingerprint", "idx_fingerprint"), ("content_key", "idx_content_key")):
        if col not in cols:
            con.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT DEFAULT ''")
            con.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON items({col})")
            con.commit()
            print(f"已补 {col} 列")

    rows = [dict(r) for r in con.execute(
        "SELECT id, platform, title, local_path, original_url, fingerprint, content_key "
        "FROM items ORDER BY id")]
    if args.platform:
        rows = [r for r in rows if r["platform"] == args.platform]

    # ── 1. 重算 content_key + fingerprint（顺带回填 DB）────────────
    ck_upd = fp_upd = nofile = 0
    for r in rows:
        ck = content_key_from_url(r["original_url"] or "", resolve=args.resolve_short)
        if ck and ck != (r["content_key"] or ""):
            r["content_key"] = ck
            ck_upd += 1
        rel = (r["local_path"] or "").strip()
        full = os.path.join(MEDIA, rel) if rel else ""
        fp = media_fingerprint(full) if full and os.path.isdir(full) else ""
        if fp:
            if fp != (r["fingerprint"] or ""):
                r["fingerprint"] = fp
                fp_upd += 1
        else:
            nofile += 1  # 读不到就沿用旧值，不拿空串覆盖
    if not args.dry_run:
        for r in rows:
            con.execute("UPDATE items SET content_key=?, fingerprint=? WHERE id=?",
                        (r["content_key"] or "", r["fingerprint"] or "", r["id"]))
        con.commit()

    # ── 2. 并查集分组（content_key / fingerprint / local_path 任一相同）──
    dsu = DSU([r["id"] for r in rows])
    first_by = {}
    for r in rows:
        keys = []
        if r["content_key"]:
            keys.append(("ck", r["content_key"]))
        if r["fingerprint"]:
            keys.append(("fp", r["fingerprint"]))
        rel = (r["local_path"] or "").strip()
        if rel:
            keys.append(("path", r["platform"], rel))
        for k in keys:
            if k in first_by:
                dsu.union(first_by[k], r["id"])
            else:
                first_by[k] = r["id"]

    comps = defaultdict(list)
    for r in rows:
        comps[dsu.find(r["id"])].append(r)
    dup_groups = {k: sorted(v, key=lambda x: x["id"]) for k, v in comps.items() if len(v) > 1}

    path_users = defaultdict(list)
    for r in rows:
        path_users[(r["platform"], (r["local_path"] or "").strip())].append(r["id"])

    plan = []          # (keep_row, drop_row, action, why)
    freed = 0
    for _root, items in sorted(dup_groups.items(), key=lambda kv: kv[1][0]["id"]):
        keep = items[0]
        for r in items[1:]:
            rel = (r["local_path"] or "").strip()
            keep_rel = (keep["local_path"] or "").strip()
            full = os.path.join(MEDIA, rel) if rel else ""
            others = [i for i in path_users.get((r["platform"], rel), [])
                      if i not in (r["id"], keep["id"])]
            if r["content_key"] and r["content_key"] == keep["content_key"]:
                why = "content_key"
            elif r["fingerprint"] and r["fingerprint"] == keep["fingerprint"]:
                why = "fingerprint"
            else:
                why = "same_dir"
            if not rel or rel == keep_rel or others:
                action = "drop_row_only"
            elif os.path.isdir(full):
                action = "move_dir+drop"
                freed += dir_size(full)
            else:
                action = "drop_row_only"
            plan.append((keep, r, action, why))

    by_plat = defaultdict(int)
    by_why = defaultdict(int)
    for keep, drop, _a, why in plan:
        by_plat[drop["platform"]] += 1
        by_why[why] += 1

    print(f"总条目 {len(rows)}｜有指纹 {sum(1 for r in rows if r['fingerprint'])}"
          f"｜有 content_key {sum(1 for r in rows if r['content_key'])}｜无媒体/读不到 {nofile}")
    print(f"重复组 {len(dup_groups)}｜准备删除 {len(plan)} 条｜预计释放 {freed / 1e9:.2f} GB")
    print("按平台:", dict(by_plat), "｜按判据:", dict(by_why))
    if not args.dry_run:
        print(f"回填: content_key {ck_upd} 条，fingerprint {fp_upd} 条")

    print("\n样例（保留 -> 删除）:")
    for keep, drop, action, why in plan[:12]:
        print(f"  [{why}] keep#{keep['id']} {keep['platform']}/{keep['title'][:24]!r}"
              f"  drop#{drop['id']} {drop['local_path']}  {action}")

    report = {
        "dry_run": args.dry_run, "total": len(rows), "groups": len(dup_groups),
        "removed": len(plan), "freed_gb": round(freed / 1e9, 3),
        "by_platform": dict(by_plat), "by_reason": dict(by_why),
        "backfill": {"content_key": ck_upd, "fingerprint": fp_upd},
        "detail": [{"keep": k["id"], "drop": d["id"], "platform": d["platform"],
                    "title": d["title"], "path": d["local_path"], "action": a, "why": w}
                   for k, d, a, w in plan],
    }

    if args.dry_run:
        print("\n[dry-run] 未做任何修改")
    else:
        moved = dropped = 0
        for keep, drop, action, _why in plan:
            rel = (drop["local_path"] or "").strip()
            if action.startswith("move_dir"):
                full = os.path.join(MEDIA, rel)
                dst = trash_duplicate(full, drop["platform"], tag=f"dup_of_{keep['id']}")
                if dst:
                    moved += 1
                else:
                    # 搬不动就别删行，留给人看（宁可留重复也不丢内容）
                    report.setdefault("move_failed", []).append({"id": drop["id"], "path": rel})
                    continue
            con.execute("DELETE FROM items WHERE id=?", (drop["id"],))
            dropped += 1
        con.commit()
        print(f"\n完成：删除 {dropped} 条，目录移入回收站 {moved} 个 -> {TRASH}")
        report["dropped"] = dropped
        report["moved_dirs"] = moved

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"报告: {args.report}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
