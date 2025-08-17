import azure.functions as func
import logging
import json
from datetime import datetime
from general_agent.general_agent import call_openai

app = func.FunctionApp()

@app.function_name(name="HttpExample")
@app.route(route="HttpExample")
def http_example(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    # Get the HTTP method
    method = req.method
    
    # Get query parameters
    name = req.params.get('name')
    prompt = req.params.get('prompt')  # New parameter for OpenAI prompts
    
    # Get request body for POST requests
    if method == "POST":
        try:
            req_body = req.get_json()
            name = req_body.get('name')
            prompt = req_body.get('prompt', prompt)  # Get prompt from body if not in query
        except ValueError:
            pass

    # If a prompt is provided, use OpenAI to generate a response
    if prompt:
        try:
            ai_response = call_openai(prompt)
            if ai_response:
                response_data = {
                    "prompt": prompt,
                    "ai_response": ai_response,
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
                    "error": "Failed to get response from OpenAI",
                    "prompt": prompt,
                    "timestamp": datetime.utcnow().isoformat(),
                    "method": method
                }
                return func.HttpResponse(
                    json.dumps(response_data),
                    status_code=500,
                    mimetype="application/json"
                )
        except Exception as e:
            response_data = {
                "error": f"Error calling OpenAI: {str(e)}",
                "prompt": prompt,
                "timestamp": datetime.utcnow().isoformat(),
                "method": method
            }
            return func.HttpResponse(
                json.dumps(response_data),
                status_code=500,
                mimetype="application/json"
            )

    # Original functionality for name parameter
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
            "message": "Please pass a name on the query string or in the request body, or pass a prompt for AI response",
            "timestamp": datetime.utcnow().isoformat(),
            "method": method
        }
        return func.HttpResponse(
            json.dumps(response_data),
            status_code=400,
            mimetype="application/json"
        )
