# syntax=docker/dockerfile:1
#
# AI Pin backend image — used for both the `app` (uvicorn) and `worker`
# (listener/worker.py) services in docker-compose.yml.
#
# python:3.11-slim is multi-arch (the Oracle VM is linux/arm64). 3.11 is pinned
# on purpose: app/audio_codec.py and app/developer_ws/tts.py import the stdlib
# `audioop` module, which was removed in Python 3.13.

# ---- builder: install Python deps into a venv --------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Compilers only exist in this stage, for any dependency without an arm64 wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# ---- runtime -------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# libopus0:  opuslib dlopens libopus.so.0 at import time (app/audio_codec.py);
#            without it the app silently falls back to raw PCM.
# libatomic1: vosk's bundled libvosk.so links libatomic.so.1 on arm64; without
#            it `import vosk` fails and STT silently returns "".
# libgomp1:  OpenMP runtime used by the vosk / onnxruntime native wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopus0 libatomic1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app/ ./app/
COPY listener/ ./listener/
# Job-queue schema; listener/worker.py applies it (CREATE ... IF NOT EXISTS) at startup.
COPY deploy/sql/ ./deploy/sql/
# main.py mounts <repo>/agent_directory at "/" and Starlette raises at import if
# the directory is missing, so it is baked in (and bind-mounted at runtime).
COPY agent_directory/ ./agent_directory/

RUN useradd --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# start-period covers the Vosk + Piper preload done in the lifespan hook.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=5)" || exit 1

# The app's modules use bare top-level imports (`from routes ...`,
# `from gemini_config import ...`), so uvicorn must run from inside app/ —
# exactly what startup.sh did on App Service. Shell form so $PORT expands;
# `exec` makes uvicorn PID 1 for clean signal handling.
WORKDIR /app/app
CMD exec python -m uvicorn main:app \
        --host 0.0.0.0 --port "${PORT}" \
        --ws websockets --ws-ping-interval 45 --ws-ping-timeout 120 \
        --proxy-headers --forwarded-allow-ips "*"
