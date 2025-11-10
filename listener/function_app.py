import azure.functions as func
import datetime
import json
import logging

app = func.FunctionApp()

@app.service_bus_queue_trigger(arg_name="msg", 
                                queue_name="q1",
                                connection="AzureWebJobsServiceBus")
def QueueWorker(msg: func.ServiceBusMessage):
    logging.info('Python HTTP trigger function processed a request.')
    print('Python HTTP trigger function processed a request.')
    body = msg.get_body().decode("utf-8")
    logging.info(f"Received message: {body}")
    
    # Process the message body
    try:
        message_data = json.loads(body)
        logging.info(f"Parsed message data: {message_data}")
        print(f"Parsed message data: {message_data}")
    except json.JSONDecodeError:
        logging.info(f"Message is not JSON, raw body: {body}")
        print(f"Message is not JSON, raw body: {body}")