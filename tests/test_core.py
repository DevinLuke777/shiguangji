#!/usr/bin/env python3
"""
拾光集 · 核心单元测试
运行: python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import clean_title, sanitize_filename, pick_stream, platform_label, is_video, is_image, parse_xhs_meta, AvatarCache


class TestCleanTitle(unittest.TestCase):
    """clean_title 标题清洗"""

    def test_normal(self):
        self.assertEqual(clean_title("普通标题"), "普通标题")

    def test_strip_leading_special(self):
        self.assertEqual(clean_title("【】#标题"), "标题")

    def test_remove_newlines(self):
        self.assertEqual(clean_title("带\n换行"), "带 换行")

    def test_remove_trailing_hashtags(self):
        self.assertEqual(clean_title("标题 #话题 #另一话题"), "标题")

    def test_truncate_80(self):
        result = clean_title("字" * 100)
        self.assertEqual(len(result), 80)

    def test_empty(self):
        self.assertEqual(clean_title(""), "")
        self.assertEqual(clean_title(None), "")

    def test_leading_dash(self):
        # 作者名以「-」开头的场景
        self.assertEqual(clean_title("-哇咔咔咔咔"), "哇咔咔咔咔")


class TestSanitizeFilename(unittest.TestCase):
    """sanitize_filename 文件名过滤"""

    def test_normal(self):
        self.assertEqual(sanitize_filename("正常名字"), "正常名字")

    def test_remove_illegal(self):
        result = sanitize_filename('a:b/c\\d*e?f"g<h>i|j')
        for c in ':/\\*?"<>|':
            self.assertNotIn(c, result)

    def test_max_len(self):
        self.assertEqual(len(sanitize_filename("字" * 100, max_len=60)), 60)


class TestPickStream(unittest.TestCase):
    """pick_stream 小红书流选择"""

    def test_prefer_309(self):
        html = '"masterUrl":"https://x.com/video_259.mp4","masterUrl":"https://x.com/video_309.mp4"'
        url, tag = pick_stream(html)
        self.assertEqual(tag, "309")
        self.assertIn("309", url)

    def test_fallback_258(self):
        html = '"masterUrl":"https://x.com/video_258.mp4"'
        url, tag = pick_stream(html)
        self.assertEqual(tag, "258")

    def test_fallback_259(self):
        html = '"masterUrl":"https://x.com/video_259.mp4"'
        url, tag = pick_stream(html)
        self.assertEqual(tag, "259")

    def test_none(self):
        url, tag = pick_stream("no stream here")
        self.assertIsNone(url)
        self.assertIsNone(tag)

    def test_decode_u002f(self):
        html = '"masterUrl":"https:\\u002F\\u002Fx.com\\u002Fvideo_309.mp4"'
        url, tag = pick_stream(html)
        self.assertEqual(url, "https://x.com/video_309.mp4")


class TestFileTypes(unittest.TestCase):
    def test_is_video(self):
        self.assertTrue(is_video("a.mp4"))
        self.assertTrue(is_video("a.MOV"))
        self.assertFalse(is_video("a.jpg"))

    def test_is_image(self):
        self.assertTrue(is_image("a.jpg"))
        self.assertTrue(is_image("a.PNG"))
        self.assertFalse(is_image("a.mp4"))


class TestPlatformLabel(unittest.TestCase):
    def test_x(self):
        self.assertEqual(platform_label("X"), "推特")

    def test_other(self):
        self.assertEqual(platform_label("抖音"), "抖音")


class TestParseXhsMeta(unittest.TestCase):
    """小红书页面元数据解析"""

    def test_full(self):
        html = '<title>测试笔记标题 - 小红书</title>"nickname":"测试作者""avatar":"https:\\u002F\\u002Fx.com\\u002Fa.jpg""desc":"正文内容""time":1730000000"masterUrl":"https://x.com/v_309.mp4"'
        meta = parse_xhs_meta(html)
        self.assertEqual(meta["title"], "测试笔记标题")
        self.assertEqual(meta["author_name"], "测试作者")
        self.assertEqual(meta["author_avatar"], "https://x.com/a.jpg")
        self.assertEqual(meta["content"], "正文内容")
        self.assertTrue(meta["has_video"])

    def test_fallback_title(self):
        html = '"title":"fallback标题""nickname":"作者"'
        meta = parse_xhs_meta(html)
        self.assertEqual(meta["title"], "fallback标题")

    def test_no_video(self):
        html = '<title>图文笔记 - 小红书</title>"nickname":"作者"'
        meta = parse_xhs_meta(html)
        self.assertFalse(meta["has_video"])


class TestAvatarCache(unittest.TestCase):
    """头像缓存 LRU + TTL"""

    def test_set_get(self):
        cache = AvatarCache(max_size=3, ttl=3600)
        cache.set("a", b"data1")
        self.assertEqual(cache.get("a"), b"data1")

    def test_lru_eviction(self):
        cache = AvatarCache(max_size=2, ttl=3600)
        cache.set("a", b"1")
        cache.set("b", b"2")
        cache.get("a")  # a 移到末尾
        cache.set("c", b"3")  # 淘汰最旧的 b
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), b"1")
        self.assertEqual(cache.get("c"), b"3")

    def test_ttl_expiry(self):
        import time as _time
        cache = AvatarCache(max_size=10, ttl=0)  # 立即过期
        cache.set("a", b"1")
        _time.sleep(0.1)
        self.assertIsNone(cache.get("a"))

    def test_miss(self):
        cache = AvatarCache()
        self.assertIsNone(cache.get("notexist"))


if __name__ == "__main__":
    unittest.main()
