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

ZIP_FILE="deploy-$(date +%Y%m%d-%H%M%S).zip"
STARTUP_CMD='bash -c "cd app && python -m uvicorn main:app --host 0.0.0.0 --port 8000 --ws websockets --ws-ping-interval 0 --ws-ping-timeout 0"'

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
if ! command -v zip &> /dev/null; then
    echo "zip command missing. Install first."
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

# App settings: env vars + build-on-deploy so requirements.txt installs
echo "Setting app settings (env vars + build flags)..."
az webapp config appsettings set \
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
    --output none

# Build deployment zip (only what runtime needs)
echo "Building deployment zip: $ZIP_FILE"
zip -rq "$ZIP_FILE" \
    app \
    requirements.txt \
    -x "*/__pycache__/*" "*.pyc" "*/.pytest_cache/*"

# Push zip to Kudu
echo "Deploying zip to App Service..."
az webapp deploy \
    --name "$APP_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --src-path "$ZIP_FILE" \
    --type zip \
    --output none

rm -f "$ZIP_FILE"

# Output URLs
APP_URL=$(az webapp show --name "$APP_NAME" --resource-group "$RESOURCE_GROUP" --query "defaultHostName" --output tsv)
echo "Deployment complete."
echo "App:       https://$APP_URL"
echo "WebSocket: wss://$APP_URL/ws"
echo "Health:    https://$APP_URL/healthz"
