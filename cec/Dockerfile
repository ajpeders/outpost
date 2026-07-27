FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8020

# cec-ctl (v4l-utils) talks straight to the kernel /dev/cec0 device (vc4/KMS,
# passed through from the host in compose) — no libCEC daemon cold-start, so
# commands land in well under a second instead of ~10s.
RUN apt-get update && apt-get install -y --no-install-recommends \
        v4l-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8020}
