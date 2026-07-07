FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8020

# libCEC + the cec-client CLI we shell out to. On a Pi 5 the CEC bus is the
# kernel /dev/cec0 device (vc4/KMS) — passed through from the host in compose.
# cec-utils pulls the right libcec (libcec7 on Debian trixie, RPi-patched).
RUN apt-get update && apt-get install -y --no-install-recommends \
        cec-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8020}
