#!/usr/bin/env python3
"""
拾光集 · 队列管理模块
用文件锁保护 JSON 队列读写，防止并发竞态。
"""
import json
import os
import re
import sys
import time
from typing import List, Dict, Any, Optional
from config import QUEUE_FILE, QUEUE_LOCK
from logger import log

# 跨平台文件锁：Linux/Mac 用 fcntl，Windows 用 msvcrt
_IS_WIN = sys.platform == "win32"
if not _IS_WIN:
    import fcntl


def _lock():
    """获取队列文件锁（跨平台）"""
    lock_fd = open(QUEUE_LOCK, "w")
    if _IS_WIN:
        import msvcrt
        # Windows 用 LK_NBLCK 重试，避免死锁
        retries = 0
        while retries < 100:
            try:
                msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                time.sleep(0.1)
                retries += 1
    else:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


def _unlock(lock_fd):
    """释放队列文件锁"""
    if _IS_WIN:
        import msvcrt
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    else:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    try:
        os.remove(QUEUE_LOCK)
    except Exception:
        pass


def load_queue() -> List[Dict[str, Any]]:
    """读取队列"""
    if not os.path.isfile(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception as e:
        log.warning(f"队列读取失败: {e}")
        return []


def save_queue(q: List[Dict[str, Any]]) -> None:
    """原子写入队列"""
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_FILE)


def _detect_url_type(url: str) -> str:
    """判断 URL 类型：profile（主页）或 single（单条）"""
    profile_patterns = [
        r"tiktok\.com/@[\w.]+/?$",           # TikTok 主页
        r"weibo\.com/u/\d+/?$",              # 微博主页
        r"weibo\.com/[\w]+/?$",              # 微博主页（用户名）
        r"m\.weibo\.cn/profile/\d+",         # 微博移动版主页
        r"xiaohongshu\.com/user/profile/",   # 小红书主页
        r"douyin\.com/(?:share/)?user/",     # 抖音主页
        r"iesdouyin\.com/share/user/",       # 抖音分享主页（短链展开后的形态）
        r"x\.com/[\w]+/?$",                  # X 主页（无 /status/）
        r"twitter\.com/[\w]+/?$",            # Twitter 主页
    ]
    for pat in profile_patterns:
        if re.search(pat, url):
            return "profile"
    return "single"


def add_to_queue(url: str) -> Dict[str, Any]:
    """添加链接到队列（带去重和锁保护）。
    自动识别主页链接 → type=profile；单条 → type=single。
    失败过的同一链接再投一次 = 重置重试，不再堆重复条目。"""
    lock_fd = _lock()
    try:
        q = load_queue()
        item_type = _detect_url_type(url)
        for item in q:
            if item.get("url") == url or item.get("original_url") == url:
                if item.get("status") in ("pending", "processing"):
                    return {"ok": False, "msg": "该链接已在队列中", "item": item}
                if item.get("status") == "failed":
                    item["status"] = "pending"
                    item["url"] = url
                    item["type"] = item_type
                    item["retries"] = 0
                    item["message"] = "重新入队"
                    item["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_queue(q)
                    return {"ok": True, "msg": "已重新入队（重置原失败记录）", "item": item}
        item = {
            "id": int(time.time() * 1000),
            "url": url,
            "original_url": url,
            "status": "pending",
            "type": item_type,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        q.append(item)
        save_queue(q)
        return {"ok": True, "item": item}
    finally:
        _unlock(lock_fd)


def add_sub_tasks(parent_id: int, urls: List[str], platform: str) -> int:
    """批量加入子任务（主页展开后调用）。返回实际加入数量。"""
    lock_fd = _lock()
    try:
        q = load_queue()
        existing_urls = {it.get("url") for it in q}
        added = 0
        for i, u in enumerate(urls):
            if u in existing_urls:
                continue
            existing_urls.add(u)
            q.append({
                "id": int(time.time() * 1000) + i,
                "url": u,
                "original_url": u,
                "status": "pending",
                "type": "single",
                "parent_id": parent_id,
                "platform_hint": platform,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            added += 1
        if added:
            save_queue(q)
        return added
    finally:
        _unlock(lock_fd)


def update_item(item_id: int, patch: Dict[str, Any]) -> bool:
    """按 id 局部更新队列条目（加锁重读，避免 worker 整表回写覆盖别人刚加的任务）"""
    lock_fd = _lock()
    try:
        q = load_queue()
        for it in q:
            if it.get("id") == item_id:
                it.update(patch)
                save_queue(q)
                return True
        return False
    finally:
        _unlock(lock_fd)


def remove_items(ids) -> int:
    """按 id 删除队列条目（加锁重读）"""
    ids = set(ids)
    lock_fd = _lock()
    try:
        q = load_queue()
        kept = [it for it in q if it.get("id") not in ids]
        n = len(q) - len(kept)
        if n:
            save_queue(kept)
        return n
    finally:
        _unlock(lock_fd)


def retry_queue_item(item_id: int) -> bool:
    """重试失败条目"""
    lock_fd = _lock()
    try:
        q = load_queue()
        for item in q:
            if item.get("id") == item_id and item.get("status") == "failed":
                item["status"] = "pending"
                item["message"] = "手动重试中..."
                save_queue(q)
                return True
        return False
    finally:
        _unlock(lock_fd)


def clear_queue() -> int:
    """清空队列，返回清除数量"""
    lock_fd = _lock()
    try:
        q = load_queue()
        count = len(q)
        save_queue([])
        return count
    finally:
        _unlock(lock_fd)


def cleanup_queue() -> None:
    """清理队列：done 删除，failed 保留 1 天"""
    lock_fd = _lock()
    try:
        q = load_queue()
        now_ts = time.time()
        kept = []
        for it in q:
            st = it.get("status")
            if st == "done":
                msg = str(it.get("message", ""))
                if msg.startswith("⚠️"):
                    try:
                        ct = time.strptime(it.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
                        if now_ts - time.mktime(ct) > 86400:
                            continue
                    except Exception:
                        pass
                    kept.append(it)
                continue
            if st == "failed":
                try:
                    ct = time.strptime(it.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
                    if now_ts - time.mktime(ct) > 86400:
                        continue
                except Exception:
                    pass
            kept.append(it)
        if len(kept) != len(q):
            save_queue(kept)
    finally:
        _unlock(lock_fd)
