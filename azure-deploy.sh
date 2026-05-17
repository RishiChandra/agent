#!/bin/bash

# Azure App Service deployment script
# Deploys this FastAPI/WebSocket app to the existing Linux App Service
# (websocket-ai-pin) via zip deploy. No Docker.

set -e

# Configuration
RESOURCE_GROUP="ai-pin"
LOCATION="westus2"
APP_NAME="websocket-ai-pin"
PLAN_NAME="ASP-aipin-9950"
PYTHON_VERSION="3.12"
SKU="B1"

# Vosk STT model + Piper TTS voice live on the App Service's persistent
# /home volume (`/home/data/...`). They are uploaded once via Kudu's
# /api/zip/data/ endpoint (~130 MB total) and reused across deploys, so we
# don't reship them in every zip. Local preflight still checks they exist on
# disk in case someone needs to re-upload.
VOSK_MODEL_DIR="vosk-model-small-en-us-0.15"
REMOTE_VOSK_PATH="/home/data/${VOSK_MODEL_DIR}"

PIPER_VOICES_DIR="piper_voices"
PIPER_VOICE_FILE="en_US-amy-medium.onnx"
REMOTE_PIPER_MODEL_PATH="/home/data/${PIPER_VOICES_DIR}/${PIPER_VOICE_FILE}"

ZIP_FILE="deploy-$(date +%Y%m%d-%H%M%S).zip"
STARTUP_CMD='bash -c "apt-get update -qq && apt-get install -y -qq libopus0 && cd app && python -m uvicorn main:app --host 0.0.0.0 --port 8000 --ws websockets"'

echo "Starting Azure App Service deployment..."

# Load env vars from .env if present
if [ -f .env ]; then
    echo "Loading .env"
    set -a
    source .env
    set +a
fi

# Preflight
if ! command -v az &> /dev/null; then
    echo "Azure CLI missing. Install first."
    exit 1
fi
if ! az account show &> /dev/null; then
    echo "Not logged in. Run 'az login' first."
    exit 1
fi
# Find a real Python interpreter. On Windows, `python` on PATH is often the
# Microsoft Store stub — it exits with "Python was not found..." instead of
# running. Verify each candidate by actually invoking --version and matching
# its output, so the stub is skipped.
PYTHON_BIN=""
for _cand in \
    "$PYTHON" \
    "$VIRTUAL_ENV/Scripts/python.exe" \
    "$VIRTUAL_ENV/bin/python" \
    "python3" \
    "python"; do
    [ -z "$_cand" ] && continue
    if "$_cand" --version 2>&1 | grep -qE "^Python [0-9]"; then
        PYTHON_BIN="$_cand"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "Could not find a working Python interpreter."
    echo "Activate your venv (e.g. 'source .venv/Scripts/activate') or set PYTHON=/path/to/python."
    exit 1
fi
echo "Using Python: $PYTHON_BIN"
if [ ! -d "$VOSK_MODEL_DIR" ] || [ ! -f "$VOSK_MODEL_DIR/am/final.mdl" ]; then
    echo "Vosk model dir missing or empty: $VOSK_MODEL_DIR"
    echo "Download it (e.g. https://alphacephei.com/vosk/models) and unpack it at the repo root."
    exit 1
fi
if [ ! -f "${PIPER_VOICES_DIR}/${PIPER_VOICE_FILE}" ] || [ ! -f "${PIPER_VOICES_DIR}/${PIPER_VOICE_FILE}.json" ]; then
    echo "Piper voice files missing in ${PIPER_VOICES_DIR}/"
    echo "Download from https://huggingface.co/rhasspy/piper-voices (e.g. en_US-amy-medium.onnx + .onnx.json)."
    exit 1
fi

echo "Azure CLI ready"

# Resource group
echo "Ensuring resource group..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# App Service plan (Linux)
if ! az appservice plan show --name "$PLAN_NAME" --resource-group "$RESOURCE_GROUP" &> /dev/null; then
    echo "Creating App Service plan ($SKU, Linux)..."
    az appservice plan create \
        --name "$PLAN_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --location "$LOCATION" \
        --sku "$SKU" \
        --is-linux \
        --output none
fi

# Web app
if ! az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" &> /dev/null; then
    echo "Creating Web App (Python $PYTHON_VERSION)..."
    az webapp create \
        --name "$APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --plan "$PLAN_NAME" \
        --runtime "PYTHON:$PYTHON_VERSION" \
        --output none
fi

# Enable WebSockets, set HTTPS-only, configure startup
echo "Configuring web app (websockets, startup, build)..."
az webapp config set \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --web-sockets-enabled true \
    --always-on true \
    --startup-file "$STARTUP_CMD" \
    --output none

az webapp update \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --https-only true \
    --output none

# App settings: env vars + build-on-deploy so requirements.txt installs.
# MSYS_NO_PATHCONV=1: Git Bash on Windows otherwise rewrites /home/... args
# to C:/Program Files/Git/home/... when passed to native az.exe.
echo "Setting app settings (env vars + build flags)..."
MSYS_NO_PATHCONV=1 az webapp config appsettings set \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --settings \
        SCM_DO_BUILD_DURING_DEPLOYMENT=true \
        ENABLE_ORYX_BUILD=true \
        WEBSITES_PORT=8000 \
        AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT}" \
        AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY}" \
        DB_HOST="${DB_HOST}" \
        DB_NAME="${DB_NAME}" \
        DB_USER="${DB_USER}" \
        DB_PASSWORD="${DB_PASSWORD}" \
        GOOGLE_API_KEY="${GOOGLE_API_KEY}" \
        AZURE_SERVICEBUS_CONNECTION_STRING="${AZURE_SERVICEBUS_CONNECTION_STRING}" \
        VOSK_MODEL_PATH="${REMOTE_VOSK_PATH}" \
        PIPER_MODEL_PATH="${REMOTE_PIPER_MODEL_PATH}" \
        ApplicationInsightsAgent_EXTENSION_VERSION="disabled" \
        XDT_MicrosoftApplicationInsights_Mode="disabled" \
        APPLICATIONINSIGHTS_CONNECTION_STRING="" \
        OTEL_SDK_DISABLED="true" \
    --output none

# Build deployment zip via Python stdlib (portable; no `zip` dependency).
echo "Building deployment zip: $ZIP_FILE"
"$PYTHON_BIN" - "$ZIP_FILE" <<'PYEOF'
import os, sys, zipfile

zip_path = sys.argv[1]
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}

def walk_into(zf, root):
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.endswith(".pyc"):
                continue
            p = os.path.join(r, f)
            zf.write(p, arcname=p.replace(os.sep, "/"))

# Vosk model + Piper voice live on /home/data on the App Service (uploaded
# once via Kudu's /api/zip/data/), so they are *not* shipped in every deploy.
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    walk_into(zf, "app")
    zf.write("requirements.txt")
PYEOF

# Push zip to Kudu.
# --track-status false avoids the CLI returning 504 while Azure is still
# building. Deployment continues server-side; status is polled separately below.
echo "Deploying zip to App Service..."
az webapp deploy \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --src-path "$ZIP_FILE" \
    --type zip \
    --track-status false \
    --output none

# Wait for the build/deploy to finish on Azure, then report final status.
echo "Waiting for deployment to complete on Azure..."
DEPLOY_DEADLINE=$(( $(date +%s) + 900 ))  # 15 minutes
while [ "$(date +%s)" -lt "$DEPLOY_DEADLINE" ]; do
    STATUS=$(az webapp log deployment list \
        --name "$APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --query "[0].status" -o tsv 2>/dev/null || echo "?")
    case "$STATUS" in
        4) echo "  status=Success"; break ;;
        3) echo "  status=Failed — check the log_url from 'az webapp log deployment list'"; break ;;
        *) echo "  status=$STATUS (pending/building/deploying); sleeping 15s..." ;;
    esac
    sleep 15
done

rm -f "$ZIP_FILE"

# Output URLs
APP_URL=$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query "defaultHostName" --output tsv)
echo "Deployment complete."
echo "App:       https://$APP_URL"
echo "WebSocket: wss://$APP_URL/ws"
echo "Health:    https://$APP_URL/healthz"
