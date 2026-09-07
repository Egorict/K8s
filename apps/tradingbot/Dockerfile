FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow \
    BOT_DATA_DIR=/data \
    BOT_WEB=1 \
    BOT_WEB_PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY tests/ ./tests/
COPY run.py ./

# Журналы пишутся в том, примонтированный в /data - переживают перезапуск пода.
RUN mkdir -p /data && useradd -r -u 10001 bot && chown -R bot:bot /app /data
USER bot

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "run.py"]
