#!/bin/bash

# Azure Container Apps deployment script
# Make sure you have Azure CLI installed and are logged in

set -e

# Configuration
RESOURCE_GROUP="ai-pin"
LOCATION="westus2"
CONTAINER_APP_NAME="websocket-ai-pin"
IMAGE_NAME="websocket-ai-pin-image"
REGISTRY_NAME="aipinregistry"

echo "🚀 Starting Azure Container Apps deployment..."

# Check if Azure CLI is installed
if ! command -v az &> /dev/null; then
    echo "❌ Azure CLI is not installed. Please install it first."
    exit 1
fi

# Check if logged in to Azure
if ! az account show &> /dev/null; then
    echo "❌ Not logged in to Azure. Please run 'az login' first."
    exit 1
fi

echo "✅ Azure CLI is ready"

# Create resource group if it doesn't exist
echo "📦 Creating resource group..."
az group create --name $RESOURCE_GROUP --location $LOCATION --output none

# Create container registry if it doesn't exist
echo "🏗️ Creating container registry..."
az acr create --resource-group $RESOURCE_GROUP --name $REGISTRY_NAME --sku Basic --output none

# Enable admin user for the registry
echo "🔑 Enabling admin user for registry..."
az acr update -n $REGISTRY_NAME --admin-enabled true

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

# Create Container Apps environment if it doesn't exist
echo "🌍 Creating Container Apps environment..."
az containerapp env create \
    --name "${CONTAINER_APP_NAME}-env" \
    --resource-group $RESOURCE_GROUP \
    --location $LOCATION \
    --output none

# Create the Container App
echo "🚀 Creating Container App..."
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

# Get the app URL
echo "🔗 Getting app URL..."
APP_URL=$(az containerapp show --name $CONTAINER_APP_NAME --resource-group $RESOURCE_GROUP --query "properties.configuration.ingress.fqdn" --output tsv)

echo "✅ Deployment completed successfully!"
echo "🌐 Your app is available at: https://$APP_URL"
echo "🔌 WebSocket endpoint: wss://$APP_URL/ws"
echo "🏥 Health check: https://$APP_URL/healthz"

# Optional: Set environment variables
echo "🔧 Setting environment variables..."
az containerapp update \
    --name $CONTAINER_APP_NAME \
    --resource-group $RESOURCE_GROUP \
    --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY}" \
    --output none

echo "🎉 All done! Your websocket app is now running on Azure Container Apps."
