#!/usr/bin/env bash
set -e

# App Service Linux Python image lacks libopus.so by default. Install at startup
# (runs as root on App Service) so opuslib's ctypes wrapper can dlopen it.
# Idempotent — apt-get install is a no-op if already installed.
if ! ldconfig -p | grep -q libopus.so.0; then
    echo "Installing libopus0..."
    apt-get update -q && apt-get install -y --no-install-recommends libopus0 || \
        echo "WARN: libopus0 install failed; opuslib will fall back to PCM."
fi

cd app && exec python -m uvicorn main:app \
    --host 0.0.0.0 --port "$PORT" \
    --ws websockets --ws-ping-interval 45 --ws-ping-timeout 120
