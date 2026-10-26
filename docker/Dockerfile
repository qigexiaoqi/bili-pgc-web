# syntax=docker/dockerfile:1
FROM alpine:3.20

LABEL org.opencontainers.image.title="bili-pgc-web" \
      org.opencontainers.image.description="Bilibili PGC downloader with a local web UI" \
      org.opencontainers.image.licenses="MIT"

RUN apk add --no-cache python3 ffmpeg tzdata ca-certificates \
 && rm -rf /var/cache/apk/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    BILI_HOST=0.0.0.0 \
    BILI_PORT=8737 \
    BILI_NO_BROWSER=1 \
    BILI_OUTPUT=/downloads \
    BILI_COOKIE=/config/cookies.txt \
    BILI_PROFILE=/config/browser

WORKDIR /app
COPY server.py core.py index.html /app/

VOLUME ["/downloads", "/config"]
EXPOSE 8737

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8737/healthz >/dev/null 2>&1 || exit 1

CMD ["python3", "/app/server.py"]
