FROM python:3.12-slim
# git: repository ingestion. tesseract + poppler: OCR for scanned PDFs and images.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tesseract-ocr poppler-utils \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 grantforge
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV GF_DATA_DIR=/data
RUN mkdir -p /data && chown grantforge:grantforge /data
USER grantforge
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
# One process: the background worker runs inside it (see grantforge/worker.py).
CMD ["uvicorn", "grantforge.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
