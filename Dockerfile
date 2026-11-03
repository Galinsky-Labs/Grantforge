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
# One process: the background worker runs inside it (see grantforge/worker.py).
# PORT is set by Render (default 10000); 8000 elsewhere.
CMD ["sh", "-c", "exec uvicorn grantforge.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
