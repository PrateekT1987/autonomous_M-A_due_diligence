# syntax=docker/dockerfile:1
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DOSSIER_DATA=/app/data

WORKDIR /app

# The image is also usable as a worker; both processes share the same build.
RUN groupadd --system dossier && useradd --system --gid dossier --home-dir /app dossier \
    && mkdir -p /app/data && chown -R dossier:dossier /app

COPY requirements.lock requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock

COPY dossier ./dossier
COPY web ./web
COPY README.md ./

USER dossier

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=4)"

CMD ["uvicorn", "dossier.app:app", "--host", "0.0.0.0", "--port", "8000"]
