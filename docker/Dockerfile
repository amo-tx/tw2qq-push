# tw2qq 常驻容器：Flask 面板 + 转发器 + QQ 小号协议端（NapCat，首次启动自动装到持久卷）
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/app/persist/home

# QQ NT（Electron）运行所需共享库 + Xvfb 虚拟显示 + ffmpeg（GIF 转码兜底）+ 中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb xauth \
        libnss3 libgbm1 libasound2 libgcrypt20 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libpango-1.0-0 libcairo2 libgtk-3-0 \
        ffmpeg fonts-wqy-zenhei procps curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/
COPY docker/config.docker.yaml /app/config.yaml

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /app/persist/home /app/data /app/logs

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
