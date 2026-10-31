#!/usr/bin/env python3
"""
拾光集 · 数据库初始化脚本
"""
import os
from config import DB_PATH
from db import init_db, migrate_db
from logger import log

if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    init_db()
    migrate_db()
    log.info(f"✅ 数据库已就绪: {DB_PATH}")
