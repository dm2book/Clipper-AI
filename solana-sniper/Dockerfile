FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    SNIPER_MIGRATIONS=/app/migrations \
    SNIPER_CONFIG=/app/config.yaml

WORKDIR /app

RUN pip install \
      "asyncpg>=0.29,<1" "httpx>=0.27,<1" "pydantic>=2.6,<3" "PyYAML>=6,<7" \
      "solders>=0.21,<1" "websockets>=12,<18"

COPY migrations ./migrations
COPY src ./src

RUN useradd --system --uid 10001 --no-create-home sniper
USER sniper

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "sniper"]
