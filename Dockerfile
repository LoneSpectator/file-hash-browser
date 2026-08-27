FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app

RUN python -m venv /opt/venv \
    && groupadd --system --gid 10001 filehash \
    && useradd --system --uid 10001 --gid filehash --home-dir /nonexistent --shell /usr/sbin/nologin filehash \
    && mkdir -p /app /config /data /files \
    && chown -R filehash:filehash /data

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY file_hash_browser ./file_hash_browser

USER 10001:10001
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=3).read()" || exit 1

ENTRYPOINT ["python", "-m", "file_hash_browser"]
CMD ["--config", "/config/config.json"]

