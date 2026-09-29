FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDV_HOST=0.0.0.0 \
    PDV_PORT=8000 \
    PDV_DB=/data/pdv.sqlite3

WORKDIR /app
COPY app.py index.html backup.py logo-pdv2.svg ./
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --home /app app && \
    mkdir /data && chown app:app /data
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["python", "app.py"]
