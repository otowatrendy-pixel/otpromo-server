FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright TZ=Asia/Tokyo
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils poppler-data fonts-ipafont-gothic \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt && pip install --no-deps rapidocr-onnxruntime==1.2.3 && playwright install --with-deps --only-shell chromium
WORKDIR /app
COPY app/ /app/
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
