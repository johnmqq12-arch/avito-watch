# Avito Watch: браузер + локальный OCR + noVNC для ручного прохождения капчи.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99 \
    AW_PROFILE_DIR=/app/data/profile \
    AW_DB_PATH=/app/data/avito.db

# Системные зависимости: Xvfb + x11vnc + noVNC (чтобы вы могли пройти капчу руками
# из браузера телефона), шрифты и утилиты.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify \
      fonts-liberation fonts-dejavu-core \
      ca-certificates curl tini procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-ocr.txt || \
       echo "OCR-пакеты не установились — капча-картинка будет решаться вручную"

# Браузер и его системные зависимости
RUN playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

COPY avito_watch ./avito_watch
COPY config.example.yaml ./config.example.yaml
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /app/data

VOLUME ["/app/data"]
EXPOSE 6080

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import json,time,sys; d=json.load(open('/app/data/heartbeat.json')); sys.exit(0 if time.time()-d['ts'] < 300 else 1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "avito_watch", "run"]