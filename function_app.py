import azure.functions as func
import logging
import json
from datetime import datetime

app = func.FunctionApp()

@app.function_name(name="HttpExample")
@app.route(route="HttpExample")
def http_example(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # Get the HTTP method
    method = req.method
    
    # Get query parameters
    name = req.params.get('name')
    
    # Get request body for POST requests
    if method == "POST":
        try:
            req_body = req.get_json()
            name = req_body.get('name')
        except ValueError:
            pass

    # Generate response
    if name:
        response_data = {
            "message": f"Hello, {name}!",
            "timestamp": datetime.utcnow().isoformat(),
            "method": method
        }
        return func.HttpResponse(
            json.dumps(response_data),
            status_code=200,
            mimetype="application/json"
        )
    else:
        response_data = {
            "message": "Please pass a name on the query string or in the request body",
            "timestamp": datetime.utcnow().isoformat(),
            "method": method
        }
        return func.HttpResponse(
            json.dumps(response_data),
            status_code=400,
            mimetype="application/json"
        )
