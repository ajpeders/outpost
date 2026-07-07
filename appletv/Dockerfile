FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ATV_STORAGE=/data/pyatv.conf \
    PORT=8010

WORKDIR /app

# On arm64 (Raspberry Pi) pyatv's miniaudio dep has no wheel and compiles from
# source via cffi — needs a C/C++ toolchain + libatomic. Build then purge to
# keep the runtime image lean (the compiled .so is what we keep).
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libatomic1 \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

RUN mkdir -p /data

# Shell form so ${PORT} is honoured under host networking on the Pi.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8010}
