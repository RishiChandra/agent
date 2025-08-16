# Azure Function - Python HTTP Trigger

This is a simple Azure Function written in Python that responds to HTTP requests.

## Project Structure

```
.
├── function_app.py            # Main function code
├── host.json                  # Host configuration
├── local.settings.json        # Local development settings
├── requirements.txt           # Python dependencies
└── README.md                 # This file
```

## Prerequisites

- Python 3.8 or higher
- Azure Functions Core Tools (version 4.x)
- Azure Storage Emulator (for local development)

## Installation

1. Install Azure Functions Core Tools:
   ```bash
   npm install -g azure-functions-core-tools@4 --unsafe-perm true
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Running Locally

1. Start the function app:
   ```bash
   func start
   ```

2. The function will be available at:
   - GET: `http://localhost:7071/api/HttpExample?name=YourName`
   - POST: `http://localhost:7071/api/HttpExample` with JSON body: `{"name": "YourName"}`

## Testing

### GET Request
```bash
curl "http://localhost:7071/api/HttpExample?name=World"
```

### POST Request
```bash
curl -X POST "http://localhost:7071/api/HttpExample" \
     -H "Content-Type: application/json" \
     -d '{"name": "World"}'
```

## Function Features

- **HTTP Methods**: Supports both GET and POST requests
- **Query Parameters**: Accepts `name` parameter via query string
- **JSON Body**: Accepts `name` parameter via JSON body for POST requests
- **Response Format**: Returns JSON with message, timestamp, and HTTP method
- **Error Handling**: Returns appropriate error messages for missing parameters
- **Modern Syntax**: Uses Python decorators for function configuration

## Deployment

To deploy to Azure:

1. Create an Azure Function App in the Azure portal
2. Deploy using Azure Functions Core Tools:
   ```bash
   func azure functionapp publish <your-function-app-name>
   ```

## Configuration

- **Authentication**: Set to anonymous for local development
- **Route**: `/api/HttpExample`
- **Storage**: Uses development storage for local development


## Git Rules
Create a new branch: ```git checkout -b <name>```
Write code :)
Stage Changes, Commit and publish branch on cursor.
On Github UI make PR.
Get approved, merge + delete branch.

In general keep PRs as small as feasible. Minimize commit and branch complexity for everyone's sake.