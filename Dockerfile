# Targets linux/arm64 (Oracle Ampere A1) but builds cleanly on amd64 too.
FROM python:3.12-slim-bookworm

# libgl/libglib are needed by opencv, which RapidOCR pulls in for image handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 souschef
WORKDIR /app

# Dependencies first so code edits don't invalidate the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY pyproject.toml alembic.ini ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY data/seeds/ ./data/seeds/
RUN pip install --no-cache-dir --no-deps -e .

# Pre-download the models and NLTK data at build time. Without this the first
# receipt scan and the first recipe import each stall on a download, and an
# offline box would fail outright.
RUN python -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()" \
 && python -c "from ingredient_parser import parse_ingredient; parse_ingredient('1 cup flour')" \
 && chown -R souschef:souschef /app /home/souschef

USER souschef
ENV SOUSCHEF_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=40s \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["sh", "-c", "souschef init && exec uvicorn souschef.web.app:app --host 0.0.0.0 --port 8000 --forwarded-allow-ips='*'"]
