#!/bin/bash

# Simple Azure Container Apps deployment script
# Assumes you already have a resource group and container registry

set -e

# Configuration - UPDATE THESE VALUES
RESOURCE_GROUP="ai-pin"
LOCATION="westus2"
CONTAINER_APP_NAME="websocket-ai-pin"
IMAGE_NAME="websocket-ai-pin-image"
REGISTRY_NAME="aipinregistry"

echo "🚀 Starting simple Azure Container Apps deployment..."

# Check if Azure CLI is installed and logged in
if ! command -v az &> /dev/null; then
    echo "❌ Azure CLI is not installed. Please install it first."
    exit 1
fi

if ! az account show &> /dev/null; then
    echo "❌ Not logged in to Azure. Please run 'az login' first."
    exit 1
fi

echo "✅ Azure CLI is ready"

# Get registry credentials
echo "🔐 Getting registry credentials..."
REGISTRY_LOGIN_SERVER=$(az acr show --name $REGISTRY_NAME --resource-group $RESOURCE_GROUP --query "loginServer" --output tsv)
REGISTRY_USERNAME=$(az acr credential show --name $REGISTRY_NAME --query "username" --output tsv)
REGISTRY_PASSWORD=$(az acr credential show --name $REGISTRY_NAME --query "passwords[0].value" --output tsv)

echo "📝 Registry: $REGISTRY_LOGIN_SERVER"

# Build and push the Docker image
echo "🐳 Building and pushing Docker image..."
docker build --platform linux/amd64 -t $IMAGE_NAME .
docker tag $IMAGE_NAME $REGISTRY_LOGIN_SERVER/$IMAGE_NAME:latest

echo "🔐 Logging into container registry..."
echo $REGISTRY_PASSWORD | docker login $REGISTRY_LOGIN_SERVER -u $REGISTRY_USERNAME --password-stdin

echo "📤 Pushing image to registry..."
docker push $REGISTRY_LOGIN_SERVER/$IMAGE_NAME:latest

# Check if Container App exists, if not create it
if ! az containerapp show --name $CONTAINER_APP_NAME --resource-group $RESOURCE_GROUP &> /dev/null; then
    echo "🚀 Creating new Container App..."
    
    # Create Container Apps environment if it doesn't exist
    if ! az containerapp env show --name "${CONTAINER_APP_NAME}-env" --resource-group $RESOURCE_GROUP &> /dev/null; then
        echo "🌍 Creating Container Apps environment..."
        az containerapp env create \
            --name "${CONTAINER_APP_NAME}-env" \
            --resource-group $RESOURCE_GROUP \
            --location "eastus" \
            --output none
    fi
    
    # Create the Container App
    az containerapp create \
        --name $CONTAINER_APP_NAME \
        --resource-group $RESOURCE_GROUP \
        --environment "${CONTAINER_APP_NAME}-env" \
        --image $REGISTRY_LOGIN_SERVER/$IMAGE_NAME:latest \
        --target-port 8000 \
        --ingress external \
        --registry-server $REGISTRY_LOGIN_SERVER \
        --registry-username $REGISTRY_USERNAME \
        --registry-password $REGISTRY_PASSWORD \
        --cpu 0.5 \
        --memory 1.0Gi \
        --min-replicas 1 \
        --max-replicas 3 \
        --output none
else
    echo "🔄 Updating existing Container App..."
    az containerapp update \
        --name $CONTAINER_APP_NAME \
        --resource-group $RESOURCE_GROUP \
        --image $REGISTRY_LOGIN_SERVER/$IMAGE_NAME:latest \
        --output none
fi

# Set environment variables
echo "🔧 Setting environment variables..."
az containerapp update \
    --name $CONTAINER_APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY}" \
    --output none

# Get the app URL
echo "🔗 Getting app URL..."
APP_URL=$(az containerapp show --name $CONTAINER_APP_NAME --resource-group $RESOURCE_GROUP --query "properties.configuration.ingress.fqdn" --output tsv)

echo "✅ Deployment completed successfully!"
echo "🌐 Your app is available at: https://$APP_URL"
echo "🔌 WebSocket endpoint: wss://$APP_URL/ws"
echo "🏥 Health check: https://$APP_URL/healthz"
echo "🎉 Your websocket app is now running on Azure Container Apps!"
