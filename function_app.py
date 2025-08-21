import azure.functions as func
import logging
import json
from datetime import datetime
from general_agent.general_agent import call_openai
from pub_sub_agent.pub_sub_agent import handle_audio_message

app = func.FunctionApp()


@app.function_name(name="AudioMessageProcessor")
@app.web_pub_sub_trigger(
    arg_name="req",
    hub="web-pub-sub-ai-pin",
    event_types=["message"],
    event_name="message",
    route="/api/webpubsub"
)
@app.web_pub_sub_output(
    arg_name="actions",
    hub="web-pub-sub-ai-pin",
    connection="WebPubSubConnectionString"
)
async def process_audio_message(req: func.WebPubSubEvent, actions: func.WebPubSubAction):
    """Process audio messages from Web PubSub and generate AI responses"""
    try:
        message = req.data
        if isinstance(message, str):
            message = json.loads(message)
        await handle_audio_message(message, actions)
    except Exception as e:
        logging.error(f"Error processing audio message: {str(e)}")
        await actions.send_to_all(f"Error: {str(e)}")


@app.function_name(name="OpenAIHttpTrigger")
@app.route(route="openai")
def openai_http_trigger(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP trigger for OpenAI chat completions"""
    logging.info('OpenAI HTTP trigger function processed a request.')

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
