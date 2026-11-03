#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拾光集 · 条目路径体检 / 修复

解决两类「页面有标题没视频」：
  1) 路径对不上：下载器重名加了 _2/_3，或轻解析落盘目录名带「：」冒号/#话题标签，
     与 DB 的 local_path 不一致 -> 按实际目录名纠正 local_path + cover_path。
  2) 根本没有目录/目录里没媒体：空标题清洗后为空导致落盘进「平台/日期/」，
     DB 指向日期目录（假条目）-> 删行（可选重新入队，worker 自带 3 次重试）。

用法:
  python3 fix_paths.py --dry-run              # 只报告
  python3 fix_paths.py                        # 修正路径（不动文件）
  python3 fix_paths.py --broken list          # 列出坏条目
  python3 fix_paths.py --broken drop          # 删除坏条目（不删任何文件）
  python3 fix_paths.py --broken requeue       # 删除坏条目 + 重新入队下载
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import sys

DB = os.environ.get("DB_PATH", "/vol1/1000/Docker/shiguangji/media_library.db")
MEDIA = os.environ.get("MEDIA_ROOT", "/vol1/1000/Downloads/拾光集")
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
VID_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
THUMB = "_thumb.jpg"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def norm(s: str) -> str:
    """归一化：小写、去空白/标点/emoji/#号，只留中英文数字

    ⚠️ 不能整段去掉 #话题标签：抖音目录名常常整串都是 #标签
    （#快问快答#你会怎么选 #二选一挑战），去掉就变空串匹配失败。
    """
    s = (s or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)
    return s


def media_files(full: str):
    try:
        names = os.listdir(full)
    except (PermissionError, OSError):
        return []
    return [n for n in names
            if not n.startswith(".") and n != THUMB
            and n.lower().endswith(IMG_EXT + VID_EXT)]


def cover_for(full_rel: str) -> str:
    full = os.path.join(MEDIA, full_rel)
    if not os.path.isdir(full):
        return ""
    names = sorted(os.listdir(full))
    if THUMB in names:
        return os.path.join(full_rel, THUMB).replace("\\", "/")
    imgs = [n for n in names if n.lower().endswith(IMG_EXT) and n != THUMB]
    if imgs:
        return os.path.join(full_rel, imgs[0]).replace("\\", "/")
    vids = [n for n in names if n.lower().endswith(VID_EXT)]
    if vids:
        return os.path.join(full_rel, THUMB).replace("\\", "/")
    return ""


def media_type_for(full_rel: str) -> str:
    full = os.path.join(MEDIA, full_rel)
    names = media_files(full)
    imgs = [n for n in names if n.lower().endswith(IMG_EXT)]
    vids = [n for n in names if n.lower().endswith(VID_EXT)]
    return "video" if (vids and not imgs) else "image"


def resolve_dir(rel: str, min_score: float = 0.72):
    """按实际磁盘目录找回条目目录：精确 -> 前缀 -> 归一化包含/相似。返回 (rel, score)"""
    rel = (rel or "").strip().rstrip("/")
    full = os.path.join(MEDIA, rel)
    if os.path.isdir(full) and media_files(full):
        return rel, 1.0
    parent = os.path.dirname(full)
    base = os.path.basename(full)
    try:
        cands = [d for d in os.listdir(parent) if os.path.isdir(os.path.join(parent, d))]
    except (PermissionError, OSError):
        return None, 0.0
    nb = norm(base)
    if not nb:
        return None, 0.0
    best, best_score = None, 0.0
    for d in cands:
        cand_full = os.path.join(parent, d)
        if not media_files(cand_full):
            continue
        nd = norm(d)
        if not nd:
            continue
        score = 0.0
        if nb == nd:
            score = 1.0
        elif d.startswith(base) or nd.startswith(nb):
            score = 0.95
        elif nb in nd or nd in nb:
            score = 0.9 - abs(len(nd) - len(nb)) / max(len(nd), len(nb), 1) * 0.1
        else:
            score = difflib.SequenceMatcher(None, nb, nd).ratio() * 0.85
        if score > best_score:
            best, best_score = d, score
    if best and best_score >= min_score:
        new_rel = os.path.join(os.path.dirname(rel), best).replace("\\", "/")
        return new_rel, best_score
    return None, best_score


def classify(con):
    """返回 (可修列表, 坏条目列表)。可修 = 路径对不上但目录找得回；坏 = 没有可用的媒体目录"""
    fixable, broken = [], []
    for row in con.execute("SELECT id, platform, title, local_path, original_url, media_type "
                           "FROM items ORDER BY id"):
        r = dict(row)
        rel = (r["local_path"] or "").strip().rstrip("/")
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        full = os.path.join(MEDIA, rel) if rel else ""
        has_media = bool(media_files(full)) if os.path.isdir(full) else False
        if has_media and len(parts) >= 3:
            continue
        new_rel = None
        _score = 0.0
        if len(parts) >= 3:
            # 平台/日期/条目 三段：按条目名在日期目录里找
            new_rel, _score = resolve_dir(rel)
        elif len(parts) == 2:
            # 只到日期目录 = 从没落进条目目录（空标题 bug）：拿标题拼一次再找
            title = (r["title"] or "").strip()
            if title:
                new_rel, _score = resolve_dir(os.path.join(rel, title), min_score=0.85)
        if new_rel:
            fixable.append((r, new_rel, _score))
        else:
            broken.append(r)
    return fixable, broken


def _ext_of(name: str, exts) -> str:
    """取真实扩展名：`.mp4` 这种以点开头的名字 splitext 会返回空扩展名，
    改名后会变成没有后缀的文件（页面认不出媒体类型）。"""
    low = (name or "").lower()
    for e in exts:
        if low.endswith(e):
            return e
    return os.path.splitext(low)[1]


def adopt_dirname(title: str, item_id: int) -> str:
    """给散落媒体建目录名：用标题（emoji 也保留），非法字符换全角，空则用 id"""
    t = (title or "").strip()
    t = t.replace("/", "／").replace("\\", "＼").replace(":", "：")
    t = re.sub(r"[\n\r\t]+", " ", t).strip()
    if not t or t in (".", ".."):
        t = f"条目_{item_id}"
    return t[:80]


def adopt_strays(con, dry_run: bool = False):
    """散落媒体归位：空标题那次下载把文件直接写在「平台/日期/」里（名字是 _1.png / .mp4），
    这里把它们收进一个新条节目录，条目就能正常播放（不用重新下载）。
    只在「该日期目录里只有一个坏条目」时执行，避免把别人的文件认领错。"""
    rows = [dict(r) for r in con.execute(
        "SELECT id, platform, title, local_path FROM items ORDER BY id")]
    by_date = {}
    for r in rows:
        rel = (r["local_path"] or "").strip().rstrip("/")
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        if len(parts) == 2:          # 平台/日期 = 指向日期目录的坏条目
            by_date.setdefault(rel, []).append(r)

    adopted = []
    for rel, items in by_date.items():
        if len(items) != 1:
            continue
        row = items[0]
        full_date = os.path.join(MEDIA, rel)
        if not os.path.isdir(full_date):
            continue
        try:
            names = sorted(os.listdir(full_date))
        except OSError:
            continue
        strays = [n for n in names
                  if os.path.isfile(os.path.join(full_date, n))
                  and n != THUMB and n.lower().endswith(IMG_EXT + VID_EXT)]
        if not strays:
            continue
        dirname = adopt_dirname(row["title"], row["id"])
        target_rel = f"{rel}/{dirname}"
        target_full = os.path.join(MEDIA, target_rel)
        n = 2
        while os.path.isdir(target_full) and os.listdir(target_full):
            target_rel = f"{rel}/{dirname}_{n}"
            target_full = os.path.join(MEDIA, target_rel)
            n += 1
        print(f"  ADOPT #{row['id']} {rel} -> {target_rel}（{len(strays)} 个散落文件）")
        adopted.append({"id": row["id"], "path": target_rel, "files": len(strays)})
        if dry_run:
            continue
        os.makedirs(target_full, exist_ok=True)
        imgs = [s for s in strays if s.lower().endswith(IMG_EXT)]
        vids = [s for s in strays if s.lower().endswith(VID_EXT)]
        for i, s in enumerate(imgs, 1):
            ext = _ext_of(s, IMG_EXT)
            os.rename(os.path.join(full_date, s),
                      os.path.join(target_full, f"{dirname}_{i}{ext}"))
        for i, s in enumerate(vids, 1):
            ext = _ext_of(s, VID_EXT)
            os.rename(os.path.join(full_date, s),
                      os.path.join(target_full, f"{dirname}{'' if i == 1 else f'_{i}'}{ext}"))
        con.execute("UPDATE items SET local_path=?, cover_path=?, media_type=? WHERE id=?",
                    (target_rel, cover_for(target_rel), media_type_for(target_rel), row["id"]))
    if adopted and not dry_run:
        con.commit()
    return adopted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--adopt", action="store_true",
                    help="先把散落在日期目录里的媒体收进条节目录（不用重新下载）")
    ap.add_argument("--broken", choices=["list", "drop", "requeue"], default="")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row

    report = {"adopted": [], "fixable": [], "broken": []}
    if args.adopt:
        print("散落媒体归位：")
        report["adopted"] = adopt_strays(con, args.dry_run)
        print(f"  共归位 {len(report['adopted'])} 条\n")

    fixable, broken = classify(con)

    print(f"路径对不上但能找回: {len(fixable)} 条｜无可用目录: {len(broken)} 条")
    for r, new_rel, score in fixable:
        print(f"  FIX #{r['id']} [{r['platform']}] {r['title'][:26]!r}  {r['local_path']} -> {new_rel}  ({score:.2f})")
        report["fixable"].append({"id": r["id"], "old": r["local_path"], "new": new_rel, "score": round(score, 2)})
        if not args.dry_run:
            con.execute("UPDATE items SET local_path=?, cover_path=?, media_type=? WHERE id=?",
                        (new_rel, cover_for(new_rel), media_type_for(new_rel), r["id"]))
    if fixable and not args.dry_run:
        con.commit()
        print(f"  已修正 {len(fixable)} 条 local_path/cover_path")

    if broken:
        print("\n无媒体条目:")
        for r in broken:
            print(f"  BAD #{r['id']} [{r['platform']}] {r['title'][:30]!r}  path={r['local_path']!r}  url={r['original_url']}")
            report["broken"].append({"id": r["id"], "platform": r["platform"], "title": r["title"],
                                     "path": r["local_path"], "url": r["original_url"]})

    if args.broken and not args.dry_run:
        if args.broken == "requeue":
            from queue_manager import add_to_queue
            ok = 0
            for r in broken:
                url = (r["original_url"] or "").strip()
                con.execute("DELETE FROM items WHERE id=?", (r["id"],))
                if url.startswith("http"):
                    res = add_to_queue(url)
                    if res.get("ok"):
                        ok += 1
            con.commit()
            print(f"\n已删除 {len(broken)} 条坏记录，重新入队 {ok} 条（worker 自带最多 3 次重试）")
            report["requeued"] = ok
        else:
            for r in broken:
                con.execute("DELETE FROM items WHERE id=?", (r["id"],))
            con.commit()
            print(f"\n已删除 {len(broken)} 条坏记录（未删任何文件）")

    print("\n[dry-run] 未修改" if args.dry_run else "")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"报告: {args.report}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
