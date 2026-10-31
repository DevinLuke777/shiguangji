#!/usr/bin/env python3
"""
拾光集 · 数据库层测试
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import db


class TestDB(unittest.TestCase):
    """数据库层测试（用临时数据库）"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._tmp.close()
        os.environ["DB_PATH"] = cls._tmp.name
        # 重新加载配置和 db 模块
        import importlib
        importlib.reload(config)
        importlib.reload(db)
        db.init_db()

    @classmethod
    def tearDownClass(cls):
        try:
            os.remove(cls._tmp.name)
        except Exception:
            pass

    def test_insert_and_get(self):
        row = {
            "platform": "抖音",
            "title": "测试标题",
            "content": "测试内容",
            "author_name": "测试作者",
            "author_avatar": "",
            "post_date": "2026-10-30 12:00:00",
            "original_url": "https://example.com/test1",
            "local_path": "抖音/2026-10-30/测试",
            "cover_path": "",
            "media_type": "video",
        }
        ok = db.insert_item(row)
        self.assertTrue(ok)
        item = db.get_item_by_url("https://example.com/test1")
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "测试标题")

    def test_query_pagination(self):
        # 插入多条
        for i in range(5):
            db.insert_item({
                "platform": "小红书",
                "title": f"标题{i}",
                "content": "",
                "author_name": f"作者{i}",
                "author_avatar": "",
                "post_date": "2026-10-30",
                "original_url": f"https://example.com/p{i}",
                "local_path": f"小红书/2026-10-30/条目{i}",
                "cover_path": "",
                "media_type": "image",
            })
        items, total = db.query_items(per_page=3, page=1)
        self.assertEqual(len(items), 3)
        self.assertGreaterEqual(total, 6)  # 包含上一个测试插入的

        items2, _ = db.query_items(per_page=3, page=2)
        self.assertGreaterEqual(len(items2), 3)

    def test_search(self):
        items, total = db.query_items(q="测试")
        self.assertGreater(total, 0)
        for it in items:
            # 至少一个字段包含"测试"
            fields = (it.get("title") or "") + (it.get("content") or "") + (it.get("author_name") or "")
            self.assertIn("测试", fields)

    def test_filter_platform(self):
        items, _ = db.query_items(platform="抖音")
        for it in items:
            self.assertEqual(it["platform"], "抖音")

    def test_delete(self):
        db.insert_item({
            "platform": "微博",
            "title": "待删除",
            "content": "",
            "author_name": "",
            "author_avatar": "",
            "post_date": "2026-10-30",
            "original_url": "https://example.com/delete-me",
            "local_path": "微博/2026-10-30/待删除",
            "cover_path": "",
            "media_type": "image",
        })
        item = db.get_item_by_url("https://example.com/delete-me")
        self.assertIsNotNone(item)
        path = db.delete_item(item["id"])
        self.assertEqual(path, "微博/2026-10-30/待删除")
        # 确认已删除
        self.assertIsNone(db.get_item_by_url("https://example.com/delete-me"))


if __name__ == "__main__":
    unittest.main()
