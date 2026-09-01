#!/usr/bin/env bash
# Fetch the Vosk STT model and the Piper TTS voice into ./data, which
# docker-compose.yml bind-mounts into the app container at /data
# (VOSK_MODEL_PATH / PIPER_MODEL_PATH in deploy/.env point there).
#
# Idempotent: anything already present is skipped. Downloads are atomic
# (.part file + rename) so an interrupted run never leaves a half file behind.
#
#   deploy/download_models.sh [DATA_DIR]      # default: <repo>/data
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${1:-${DATA_DIR:-$REPO_ROOT/data}}"

VOSK_MODEL="vosk-model-small-en-us-0.15"
VOSK_URL="https://alphacephei.com/vosk/models/${VOSK_MODEL}.zip"

PIPER_VOICE="en_US-amy-medium"
# huggingface.co/rhasspy/piper-voices layout: <lang>/<lang_REGION>/<speaker>/<quality>/
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium"

fetch() { # fetch URL DEST
    local url="$1" dest="$2"
    echo "    $url"
    curl -fL --retry 3 --retry-delay 5 --progress-bar -o "$dest.part" "$url"
    mv -f "$dest.part" "$dest"
}

mkdir -p "$DATA_DIR/piper_voices"

# ---- Vosk ------------------------------------------------------------------
if [ -f "$DATA_DIR/$VOSK_MODEL/am/final.mdl" ]; then
    echo "vosk:  already present: $DATA_DIR/$VOSK_MODEL"
else
    echo "vosk:  downloading $VOSK_MODEL (~40 MB)"
    ZIP="$DATA_DIR/$VOSK_MODEL.zip"
    EXTRACT="$DATA_DIR/$VOSK_MODEL.extract"
    fetch "$VOSK_URL" "$ZIP"
    rm -rf "$EXTRACT" "$DATA_DIR/$VOSK_MODEL"
    mkdir -p "$EXTRACT"
    python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$ZIP" "$EXTRACT"
    # The zip wraps everything in one top-level directory (normally named after
    # the model). Move whichever directory holds am/final.mdl into place.
    FOUND="$(find "$EXTRACT" -maxdepth 3 -type f -name final.mdl -path '*/am/*' | head -n1 || true)"
    if [ -z "$FOUND" ]; then
        echo "vosk:  unexpected zip layout, no am/final.mdl under $EXTRACT" >&2
        exit 1
    fi
    mv "$(dirname "$(dirname "$FOUND")")" "$DATA_DIR/$VOSK_MODEL"
    rm -rf "$EXTRACT" "$ZIP"
    echo "vosk:  ready: $DATA_DIR/$VOSK_MODEL"
fi

# ---- Piper -------------------------------------------------------------------
for f in "$PIPER_VOICE.onnx" "$PIPER_VOICE.onnx.json"; do
    if [ -f "$DATA_DIR/piper_voices/$f" ]; then
        echo "piper: already present: $DATA_DIR/piper_voices/$f"
    else
        echo "piper: downloading $f"
        fetch "$PIPER_BASE/$f" "$DATA_DIR/piper_voices/$f"
    fi
done
echo "piper: ready: $DATA_DIR/piper_voices/$PIPER_VOICE.onnx"

# The app container runs as an unprivileged user; make sure it can read everything.
chmod -R a+rX "$DATA_DIR"
