#!/usr/bin/env python3
# 重采集：用真实原链接重新抓元数据入库（先删 scan:// 旧记录）
import sqlite3, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ingest import ingest, DB_PATH

# (平台, 原链接, 本地相对路径) —— 从会话记录找回的原始链接
ITEMS = [
    ("抖音", "https://v.douyin.com/yeYPX1cfE3o/",
     "抖音/2026-08-08/#浩天祖 #梦的翅膀受了伤 #群像"),
    ("TikTok", "https://www.tiktok.com/@niyeye2026/photo/7671170631420923167",
     "TikTok/2026-08-08/#cosplay #nikke 伊莱格～"),
    ("X", "https://x.com/pumchm/status/2085659714285928710",
     "Twitter/2026-08-08/男性にコメント欄見られたら詰みます笑笑"),
    ("小红书", "http://xhslink.cn/o/6v8KHYfWe2d",
     "小红书/2026-08-08/才知道全冰去水是这样喝的啊！"),
    ("小红书", "http://xhslink.cn/o/2l0Jo2vUkLo",
     "小红书/2026-08-08/大幂幂真好看😍😍"),
    ("小红书", "http://xhslink.cn/o/9ic4SyhzZJ8",
     "小红书/2026-08-08/这居然是娜扎？"),
    ("微博", "https://weibo.com/1350995007/5314505703951208",
     "微博/2026-08-08/kukukukuku！～～～#生活有星意#"),
]

conn = sqlite3.connect(DB_PATH)
for _, _, path in ITEMS:
    conn.execute("DELETE FROM items WHERE local_path=?", (path,))
conn.commit()
conn.close()
print("✅ 已清除旧 scan 记录")

for platform, url, path in ITEMS:
    print(f"\n--- [{platform}] {path.split('/')[-1][:30]} ---")
    ingest(platform, url, path)
