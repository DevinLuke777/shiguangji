#!/usr/bin/env python3
"""一次性补丁：给已入库的微博条目补齐实况图视频（同名 .mov）。

不删原图，只新增「与 jpg 同名的 .mov」，让 /api/live-photo/<id> 能配成对。

用法:
  /usr/bin/python3 backfill_weibo_live.py 4267 4266 4268
  /usr/bin/python3 backfill_weibo_live.py --all       # 所有微博条目
  /usr/bin/python3 backfill_weibo_live.py --all --dry-run
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config                                   # noqa: E402
from db import get_conn                         # noqa: E402
import weibo_live as W                          # noqa: E402
from utils import make_live_photo               # noqa: E402

# 手工跑脚本时没有 systemd 的 env，兜底到真实媒体根目录
MEDIA_ROOT = config.MEDIA_ROOT
if not os.path.isdir(MEDIA_ROOT):
    for _fallback in ("/vol1/1000/Downloads/拾光集",):
        if os.path.isdir(_fallback):
            MEDIA_ROOT = _fallback
            break


def item_row(item_id):
    with get_conn() as c:
        r = c.execute("select id, title, original_url, local_path, platform "
                      "from items where id=?", (item_id,)).fetchone()
    return dict(r) if r else None


def weibo_items():
    with get_conn() as c:
        rows = c.execute("select id, title, original_url, local_path, platform "
                         "from items where platform='微博' order by id").fetchall()
    return [dict(r) for r in rows]


def backfill(item_id, dry=False):
    it = item_row(item_id)
    if not it:
        return {"id": item_id, "status": "缺失", "msg": "DB 无此条目"}
    if it["platform"] != "微博":
        return {"id": item_id, "status": "跳过", "msg": f"非微博({it['platform']})"}
    rel = it.get("local_path") or ""
    d = os.path.join(MEDIA_ROOT, rel)
    if not rel or not os.path.isdir(d):
        return {"id": item_id, "status": "失败", "msg": "目录不存在"}

    try:
        pics = W.live_pics(it["original_url"])
    except Exception as e:
        return {"id": item_id, "status": "失败", "msg": f"取帖子失败: {str(e)[:70]}"}

    live_idx = [i for i, p in enumerate(pics) if p.get("video")]
    if not live_idx:
        return {"id": item_id, "status": "无实况", "msg": f"{len(pics)} 张图, 0 实况"}

    mapping = W.match_local_jpgs(d, pics, multi=True)
    done, existed, failed, sizes = 0, 0, 0, []
    for i in live_idx:
        jpgs = mapping.get(i) or []
        if not jpgs:
            failed += 1
            continue
        master = os.path.join(d, f".live_master_{i}.mov")
        try:
            for jpg in jpgs:
                mov = os.path.splitext(jpg)[0] + ".mov"
                if os.path.isfile(mov) and os.path.getsize(mov) > 100 * 1024:
                    existed += 1
                    sizes.append(os.path.getsize(mov))
                    continue
                if dry:
                    done += 1
                    continue
                if not os.path.isfile(master):
                    if not W.download_pic(it["original_url"], master, idx=i):
                        failed += 1
                        break        # 这张图的视频取不到，后面的副本也不用试
                # 每个副本单独转一次（make_live_photo 会给 jpg/mov 写同一个 pairing UUID）
                tmp = os.path.join(os.path.dirname(jpg),
                                   f".tmp_live_{os.path.basename(jpg).rsplit('.', 1)[0][-20:]}_{i}.mov")
                with open(master, "rb") as src, open(tmp, "wb") as dst:
                    dst.write(src.read())
                out = make_live_photo(jpg, tmp)
                if os.path.isfile(out) and os.path.getsize(out) > 100 * 1024:
                    done += 1
                    sizes.append(os.path.getsize(out))
                else:
                    failed += 1
                if os.path.isfile(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
        except Exception as e:
            failed += 1
            print(f"  ! {item_id} pic{i+1}: {str(e)[:80]}")
        finally:
            if os.path.isfile(master):
                try:
                    os.remove(master)
                except Exception:
                    pass

    total = done + existed
    status = "有实况" if total else ("失败" if failed else "无实况")
    avg = (sum(sizes) // len(sizes) // 1024) if sizes else 0
    return {"id": item_id, "status": status, "live": len(live_idx),
            "new": done, "existed": existed, "failed": failed,
            "avg_kb": avg, "pics": len(pics)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    ids = [it["id"] for it in weibo_items()] if a.all else a.ids
    if not ids:
        ap.error("给条目 id 或 --all")
    rows = []
    for i in ids:
        r = backfill(i, dry=a.dry_run)
        rows.append(r)
        print(f"[{r['id']}] {r['status']} live={r.get('live','-')} "
              f"new={r.get('new','-')} existed={r.get('existed','-')} "
              f"failed={r.get('failed','-')} avg={r.get('avg_kb','-')}KB "
              f"{r.get('msg','')}")
    ok = sum(1 for r in rows if r["status"] == "有实况")
    print(f"\n汇总: {ok}/{len(rows)} 条有实况")


if __name__ == "__main__":
    main()
