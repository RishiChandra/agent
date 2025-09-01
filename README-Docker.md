# Docker Deployment to Azure Container Apps

This guide will help you deploy your FastAPI websocket application to Azure using Docker containers.

## Prerequisites

1. **Azure CLI** installed and configured
2. **Docker** installed and running
3. **Azure subscription** with appropriate permissions
4. **Google API Key** for Gemini AI

## Quick Start

### 1. Set Environment Variables

```bash
export GOOGLE_API_KEY="your-google-api-key"
```

### 2. Deploy

#### Option A: Full Deployment (Creates everything)
```bash
chmod +x azure-deploy.sh
./azure-deploy.sh
```

#### Option B: Simple Deployment (Uses existing resources)
```bash
chmod +x azure-deploy-simple.sh
./azure-deploy-simple.sh
```

## What Gets Created

- **Resource Group**: `ai-pin`
- **Azure Container Registry (ACR)**: `aipinregistry`
- **Container Apps Environment**: `websocket-ai-pin-env`
- **Container App**: `websocket-ai-pin`

## Architecture

```
Client → Azure Container Apps → Your FastAPI App → Gemini AI
```

## Configuration Details

### Container Specs
- **CPU**: 0.5 cores
- **Memory**: 1.0 GiB
- **Port**: 8000
- **Scaling**: 1-3 replicas
- **Platform**: Linux/AMD64 (required for Azure)

### Health Checks
- Endpoint: `/healthz`
- Returns: `{"ok": true}`

### WebSocket Endpoint
- Path: `/ws`
- Protocol: `wss://` (secure WebSocket)

## Local Testing

Test your Docker image locally before deploying:

```bash
# Build and run locally
docker build --platform linux/amd64 -t websocket-app .
docker run -p 8000:8000 -e GOOGLE_API_KEY=$GOOGLE_API_KEY websocket-app

# Or use the test script
chmod +x test-docker.sh
./test-docker.sh
```

## Deployment URLs

After successful deployment, your app will be available at:

- **Web App**: `https://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io`
- **Health Check**: `https://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/healthz`
- **WebSocket**: `wss://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/ws`

## Testing Your Deployment

### 1. Test Health Endpoint
```bash
curl https://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/healthz
```

### 2. Test WebSocket Connection
Use the provided test script:
```bash
cd websocket_logic/test
python3 test_ws.py
```

This will:
- Connect to your Azure WebSocket endpoint
- Test audio input/output with Gemini Live
- Verify real-time conversation capabilities

## Troubleshooting

### Common Issues

1. **Platform mismatch**: Ensure Docker build uses `--platform linux/amd64`
2. **Environment variables**: Verify GOOGLE_API_KEY is set in Azure
3. **Port conflicts**: Ensure port 8000 is available

### Debug Commands

```bash
# Check container app status
az containerapp show --name websocket-ai-pin --resource-group ai-pin

# View logs
az containerapp logs show --name websocket-ai-pin --resource-group ai-pin

# Check environment variables
az containerapp show --name websocket-ai-pin --resource-group ai-pin --query "properties.template.containers[0].env"

# Get current app URL
az containerapp show --name websocket-ai-pin --resource-group ai-pin --query "properties.configuration.ingress.fqdn" --output tsv
```

### Fixing Environment Variables

If the app crashes due to missing environment variables:

```bash
# Set Google API key
az containerapp update --name websocket-ai-pin --resource-group ai-pin --set-env-vars "GOOGLE_API_KEY=your-api-key"

# Wait for restart and test
curl https://your-app-url/healthz
```

## Cost Optimization

- **Basic ACR**: ~$0.17/day
- **Container Apps**: ~$0.50-1.50/day depending on usage
- **Total**: ~$0.67-1.67/day

## Security Notes

- Container runs as non-root user
- Health checks prevent unhealthy containers from receiving traffic
- Environment variables are securely stored in Azure
- WebSocket connections use WSS (secure)

## Features

Your deployed app includes:

- **Real-time audio conversation** with Gemini Live
- **WebSocket streaming** for low-latency communication
- **Audio transcription** (input and output)
- **Interruption handling** for natural conversation flow
- **Health monitoring** endpoint
- **Auto-scaling** based on demand

## Next Steps

After successful deployment:

1. ✅ Test your WebSocket endpoint
2. ✅ Verify health check is working
3. Monitor performance in Azure Portal
4. Set up custom domain if needed
5. Configure SSL certificates
6. Scale up/down based on usage

## Support

If you encounter issues:

1. Check Azure Container Apps logs
2. Verify environment variables are set
3. Test locally with Docker first
4. Ensure platform is linux/amd64
5. Check Azure Container Registry connectivity

## Success Indicators

Your deployment is successful when:
- Health endpoint returns `{"ok": true}`
- WebSocket connects without errors
- Audio conversation flows naturally
- Gemini responds to voice input
- Interruption handling works correctly
