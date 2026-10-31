FROM python:3.12-slim

WORKDIR /app

# 系统依赖：ffmpeg（缩略图/Live图转换）+ exiftool（Live Photo 元数据注入）
# ffmpeg/exiftool 暂不装（构建卡在 apt），需要时用 Dockerfile.with-ffmpeg 单独构建

# Python 依赖
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY config.py logger.py db.py utils.py queue_manager.py app.py ingest.py init_db.py gen_thumbs.py /app/
COPY templates /app/templates
COPY static /app/static

ENV MEDIA_ROOT=/media
ENV DB_PATH=/data/media_library.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8090

# 启动时自动建库 + 迁移
CMD ["python", "app.py"]
