FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata gosu \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /data

EXPOSE 8321

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
  CMD python /app/scripts/healthcheck.py || exit 1

# 默认仍以 root 运行（PUID/PGID 默认 0），行为不变；
# 需要非 root 运行时在 compose 里设置 PUID/PGID 环境变量即可，见 entrypoint.sh 注释。
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8321"]
