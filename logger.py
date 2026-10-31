#!/usr/bin/env python3
"""
拾光集 · 统一日志模块
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from config import LOG_DIR, LOG_LEVEL


def setup_logger(name: str = "shiguangji") -> logging.Logger:
    """创建带文件轮转和控制台输出的 logger"""
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    # 格式
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台输出
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 文件输出（轮转，最大 10MB，保留 5 个备份）
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_file = os.path.join(LOG_DIR, f"{name}.log")
        file_handler = RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except Exception as e:
        logger.warning(f"日志文件初始化失败，仅输出到控制台: {e}")

    return logger


# 全局 logger 实例
log = setup_logger()
