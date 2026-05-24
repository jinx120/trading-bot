FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Jupyter served on :8888 (start manually with `docker compose exec platform
# jupyter lab --ip=0.0.0.0 --no-browser --allow-root` until chunk 3 promotes
# it to a managed process).
EXPOSE 8888
