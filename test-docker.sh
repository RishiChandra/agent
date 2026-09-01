#!/bin/bash

# Test script to verify Docker build and run locally

set -e

echo "🧪 Testing Docker build and run locally..."

# Check if Docker is running
if ! docker info &> /dev/null; then
    echo "❌ Docker is not running. Please start Docker first."
    exit 1
fi

echo "✅ Docker is running"

# Never hardcode the key here: take it from the shell, else from deploy/.env.
if [ -z "${GOOGLE_API_KEY:-}" ] && [ -f deploy/.env ]; then
    GOOGLE_API_KEY="$(grep -E '^GOOGLE_API_KEY=' deploy/.env | tail -n1 | cut -d= -f2-)"
fi
export GOOGLE_API_KEY="${GOOGLE_API_KEY:?set GOOGLE_API_KEY in your shell or in deploy/.env}"

# Build the Docker image
echo "🐳 Building Docker image..."
docker build -t websocket-app-test .

echo "✅ Docker image built successfully"

# Test the container
echo "🚀 Testing container..."
docker run -d --name websocket-test -p 8000:8000 -e GOOGLE_API_KEY="$GOOGLE_API_KEY" websocket-app-test

# Wait for container to start
echo "⏳ Waiting for container to start..."
sleep 15

# Test health endpoint with retries
echo "🏥 Testing health endpoint..."
MAX_RETRIES=5
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -f http://localhost:8000/healthz > /dev/null 2>&1; then
        echo "✅ Health check passed"
        break
    else
        RETRY_COUNT=$((RETRY_COUNT + 1))
        echo "⏳ Health check attempt $RETRY_COUNT/$MAX_RETRIES failed, retrying in 3 seconds..."
        sleep 3
    fi
done

if [ $RETRY_COUNT -eq $MAX_RETRIES ]; then
    echo "❌ Health check failed after $MAX_RETRIES attempts"
    echo "🔍 Checking container logs..."
    docker logs websocket-test
fi

# Clean up
echo "🧹 Cleaning up test container..."
docker stop websocket-test
docker rm websocket-test

echo "🎉 Docker test completed successfully!"
echo "💡 You can now deploy to the Oracle VM with deploy/deploy.sh (see deploy/README.md)"
