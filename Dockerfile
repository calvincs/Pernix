# Reconstructed 2026-08-19 from `docker history pernix:latest` after the
# box-local original was lost to a cleanup — the image had never been
# reproducible from the repo. Keep this file in git; secrets live in the
# box-local .env consumed by docker-compose.yml, never here.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl ffmpeg python3.12-venv \
    && rm -f /usr/lib/python3.12/EXTERNALLY-MANAGED \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY vendor/ vendor/
RUN pip install --no-cache-dir -r requirements.txt yt-dlp \
    && playwright install chromium

COPY . .

EXPOSE 8090
CMD ["python", "run.py"]
