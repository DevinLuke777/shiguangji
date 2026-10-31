#!/usr/bin/env python3
"""
拾光集 · 数据库层
封装所有 SQLite 操作，替代散落的裸连接。
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any, Generator, Tuple
from config import DB_PATH
from logger import log


def get_conn() -> sqlite3.Connection:
    """获取数据库连接，启用外键约束和 WAL 模式"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor() -> Generator[sqlite3.Cursor, None, None]:
    """上下文管理器：自动 commit/rollback/close"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """初始化数据库表结构"""
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                title TEXT,
                content TEXT,
                author_name TEXT,
                author_avatar TEXT,
                post_date TEXT,
                original_url TEXT,
                local_path TEXT,
                cover_path TEXT DEFAULT '',
                media_type TEXT DEFAULT 'image',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(original_url)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_platform ON items(platform)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_date ON items(post_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_author ON items(author_name)")
        # media_type 索引需等迁移补列后再建，见 ensure_indexes()
    log.info("数据库初始化完成")


def ensure_indexes() -> None:
    """迁移完成后补建依赖新字段的索引（旧库升级路径必需）"""
    with db_cursor() as cur:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(items)").fetchall()}
        if "media_type" in cols:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_media_type ON items(media_type)")


def query_items(
    q: str = "",
    platform: str = "",
    author: str = "",
    media: str = "",
    sort: str = "created",
    direction: str = "desc",
    page: int = 1,
    per_page: int = 40,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    查询条目，支持搜索/筛选/排序/分页。
    返回 (items, total_count)
    """
    where_parts: List[str] = []
    params: List[Any] = []

    if q:
        where_parts.append("(title LIKE ? OR author_name LIKE ? OR content LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if platform:
        where_parts.append("platform=?")
        params.append(platform)
    if author:
        where_parts.append("author_name=?")
        params.append(author)
    if media in ("image", "video"):
        where_parts.append("media_type=?")
        params.append(media)

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    # 排序
    sort_col = "post_date" if sort == "post" else "created_at"
    sort_dir = "ASC" if direction == "asc" else "DESC"
    null_handling = f"({sort_col} IS NULL OR {sort_col}='') ASC," if sort_col == "post_date" else ""

    # 总数
    conn = get_conn()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM items{where_sql}", params).fetchone()[0]

        offset = (page - 1) * per_page
        sql = f"SELECT * FROM items{where_sql} ORDER BY {null_handling} {sort_col} {sort_dir} LIMIT ? OFFSET ?"
        rows = conn.execute(sql, params + [per_page, offset]).fetchall()
        items = [dict(r) for r in rows]
    finally:
        conn.close()

    return items, total


def get_item(item_id: int) -> Optional[Dict[str, Any]]:
    """按 ID 获取单条"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_item_by_url(url: str) -> Optional[Dict[str, Any]]:
    """按原始链接查询（去重用）"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, title, created_at FROM items WHERE original_url=?", (url,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insert_item(data: Dict[str, Any]) -> bool:
    """插入条目，返回是否成功"""
    try:
        with db_cursor() as cur:
            data.setdefault("src_tag", "")
            data.setdefault("wm_flag", "")
            cur.execute("""
                INSERT OR REPLACE INTO items
                (platform, title, content, author_name, author_avatar, post_date, original_url, local_path, cover_path, media_type, src_tag, wm_flag)
                VALUES (:platform, :title, :content, :author_name, :author_avatar, :post_date, :original_url, :local_path, :cover_path, :media_type, :src_tag, :wm_flag)
            """, data)
        return True
    except Exception as e:
        log.error(f"入库失败: {e}")
        return False


def delete_item(item_id: int) -> Optional[str]:
    """删除条目，返回 local_path（用于清理文件）"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT local_path FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.commit()
        return row[0]
    finally:
        conn.close()


def update_item(item_id: int, fields: Dict[str, Any]) -> bool:
    """更新条目字段"""
    if not fields:
        return False
    sets = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [item_id]
    try:
        with db_cursor() as cur:
            cur.execute(f"UPDATE items SET {sets} WHERE id=?", params)
        return True
    except Exception as e:
        log.error(f"更新条目 {item_id} 失败: {e}")
        return False


def get_platforms() -> List[str]:
    """获取所有平台列表"""
    conn = get_conn()
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT platform FROM items ORDER BY platform").fetchall()]
    finally:
        conn.close()


def get_authors() -> List[Dict[str, Any]]:
    """获取作者列表（带统计）"""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT author_name, COUNT(*) as cnt,
                   (SELECT author_avatar FROM items i2
                    WHERE i2.author_name=i.author_name AND i2.author_avatar != ''
                    LIMIT 1) as avatar
            FROM items i WHERE author_name != ''
            GROUP BY author_name ORDER BY cnt DESC
        """).fetchall()
        return [{"name": r[0], "cnt": r[1], "avatar": r[2]} for r in rows]
    finally:
        conn.close()


def is_path_referenced(rel_path: str) -> bool:
    """检查路径是否被某条目引用"""
    conn = get_conn()
    try:
        return bool(conn.execute("SELECT 1 FROM items WHERE local_path=?", (rel_path,)).fetchone())
    finally:
        conn.close()


def get_paths_in_dir(date_dir: str) -> set:
    """获取某日期目录下所有被引用的路径"""
    conn = get_conn()
    try:
        return {r[0] for r in conn.execute(
            "SELECT local_path FROM items WHERE local_path LIKE ?", (date_dir + "/%",)
        ).fetchall()}
    finally:
        conn.close()


def item_exists_by_path(rel_path: str) -> bool:
    """检查路径是否已存在"""
    return is_path_referenced(rel_path)


# ─── 数据库迁移版本管理 ──────────────────────────────────
def _get_schema_version(cur) -> int:
    """获取当前 schema 版本，无版本表则返回 0"""
    try:
        row = cur.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        # 表不存在
        return 0


def _set_schema_version(cur, version: int) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY, version INTEGER)
    """)
    cur.execute("INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)", (version,))


def migrate_db() -> None:
    """
    版本化数据库迁移。
    每个版本号对应一次结构变更，按顺序执行，不可逆。
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        current = _get_schema_version(cur)

        # v1: 初始版本（建表时的字段）
        if current < 1:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(items)").fetchall()}
            if "cover_path" not in cols:
                cur.execute("ALTER TABLE items ADD COLUMN cover_path TEXT DEFAULT ''")
            if "media_type" not in cols:
                cur.execute("ALTER TABLE items ADD COLUMN media_type TEXT DEFAULT 'image'")
            _set_schema_version(cur, 1)
            log.info("迁移: v1 — 添加 cover_path/media_type 字段")

        # v2: 补全已有条目的 cover_path（空值的用第一张图填充）
        if current < 2:
            rows = cur.execute(
                "SELECT id, local_path FROM items WHERE cover_path='' OR cover_path IS NULL"
            ).fetchall()
            for item_id, rel in rows:
                if not rel:
                    continue
                full = os.path.join(os.environ.get("MEDIA_ROOT", ""), rel)
                if not os.path.isdir(full):
                    continue
                cover = ""
                thumb = os.path.join(full, "_thumb.jpg")
                if os.path.isfile(thumb):
                    cover = os.path.join(rel, "_thumb.jpg").replace("\\", "/")
                else:
                    for f in sorted(os.listdir(full)):
                        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) and f != "_thumb.jpg":
                            cover = os.path.join(rel, f).replace("\\", "/")
                            break
                if cover:
                    cur.execute("UPDATE items SET cover_path=? WHERE id=?", (cover, item_id))
            _set_schema_version(cur, 2)
            log.info("迁移: v2 — 补全空 cover_path")

        # v3: 回填 media_type（旧库所有条目默认是 image，含视频的目录要纠正）
        if current < 3:
            video_ext = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".flv")
            media_root = os.environ.get("MEDIA_ROOT", "")
            rows = cur.execute("SELECT id, local_path FROM items").fetchall()
            changed = 0
            for item_id, rel in rows:
                if not rel or not media_root:
                    continue
                full = os.path.join(media_root, rel)
                if not os.path.isdir(full):
                    continue
                try:
                    names = os.listdir(full)
                except OSError:
                    continue
                # _thumb.jpg 是缩略图，不参与类型判断
                real = [n for n in names if not n.startswith(("_thumb", "."))]
                has_video = any(n.lower().endswith(video_ext) for n in real)
                has_image = any(n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
                                for n in real)
                mt = "video" if has_video and not has_image else "image"
                if mt != "image":
                    cur.execute("UPDATE items SET media_type=? WHERE id=?", (mt, item_id))
                    changed += 1
            _set_schema_version(cur, 3)
            log.info(f"迁移: v3 — 回填 media_type，标记视频 {changed} 条")

        # v4: 水印溯源字段（src_tag: 309/258=无水印流, 259=水印流；wm_flag: ''=未检测 ok/suspect）
        if current < 4:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(items)").fetchall()}
            if "src_tag" not in cols:
                cur.execute("ALTER TABLE items ADD COLUMN src_tag TEXT DEFAULT ''")
            if "wm_flag" not in cols:
                cur.execute("ALTER TABLE items ADD COLUMN wm_flag TEXT DEFAULT ''")
            _set_schema_version(cur, 4)
            log.info("迁移: v4 — 添加 src_tag/wm_flag 字段")


        # v5: 回填 cover_path + 纠正 media_type（按目录实际文件，修新入库为空/错标的情况）
        if current < 5:
            media_root = os.environ.get("MEDIA_ROOT", "")
            img_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif")
            vid_ext = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
            fixed_cover = fixed_type = 0
            for item_id, rel, cov, mt in cur.execute(
                    "SELECT id, local_path, cover_path, media_type FROM items").fetchall():
                if not rel or not media_root:
                    continue
                full = os.path.join(media_root, rel)
                if not os.path.isdir(full):
                    continue
                try:
                    names = [n for n in os.listdir(full) if not n.startswith(".")]
                except OSError:
                    continue
                imgs = [n for n in names if n.lower().endswith(img_ext)
                        and "_thumb" not in n.lower() and "_live" not in n.lower()]
                vids = [n for n in names if n.lower().endswith(vid_ext) and "_live" not in n.lower()]
                new_type = "video" if (vids and not imgs) else ("image" if imgs else mt)
                if new_type and new_type != mt:
                    cur.execute("UPDATE items SET media_type=? WHERE id=?", (new_type, item_id))
                    fixed_type += 1
                if not cov:
                    cover = ""
                    if "_thumb.jpg" in names:
                        cover = os.path.join(rel, "_thumb.jpg").replace("\\", "/")
                    elif imgs:
                        cover = os.path.join(rel, sorted(imgs)[0]).replace("\\", "/")
                    elif vids:
                        cover = os.path.join(rel, "_thumb.jpg").replace("\\", "/")
                    if cover:
                        cur.execute("UPDATE items SET cover_path=? WHERE id=?", (cover, item_id))
                        fixed_cover += 1
            _set_schema_version(cur, 5)
            log.info(f"迁移: v5 — 补封面 {fixed_cover} 条，纠正类型 {fixed_type} 条")

        conn.commit()
    finally:
        conn.close()


def get_stats() -> dict:
    """获取统计数据（平台分布/作者Top/趋势/总数）"""
    conn = get_conn()
    try:
        platforms = [dict(r) for r in conn.execute(
            "SELECT platform, COUNT(*) c FROM items GROUP BY platform ORDER BY c DESC"
        ).fetchall()]
        authors = [dict(r) for r in conn.execute(
            "SELECT author_name, COUNT(*) c FROM items "
            "WHERE author_name IS NOT NULL AND author_name != '' "
            "GROUP BY author_name ORDER BY c DESC LIMIT 10"
        ).fetchall()]
        trend = [dict(r) for r in conn.execute(
            "SELECT substr(created_at,1,10) d, COUNT(*) c FROM items "
            "GROUP BY d ORDER BY d DESC LIMIT 14"
        ).fetchall()]
        trend.reverse()
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        return {
            "platforms": platforms,
            "authors": authors,
            "trend": trend,
            "total": total,
        }
    finally:
        conn.close()
