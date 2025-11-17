import azure.functions as func
import datetime
import json
import logging
import sys
from pathlib import Path

from session_management_utils import get_session

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

        user_id = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
                
        try:
           session = get_session(user_id)
           if session:
                logging.info(f"FOUND SESSION FOR USER {user_id}")
                print(f"FOUND SESSION FOR USER {user_id}")
           else:
                logging.info(f"COULD NOT FIND SESSION FOR USER {user_id}")
                print(f"COULD NOT FIND SESSION FOR USER {user_id}")
        except Exception as e:
            logging.error(f"Error querying tasks table: {e}")
            print(f"Error querying tasks table: {e}")
            
    except json.JSONDecodeError:
        logging.info(f"Message is not JSON, raw body: {body}")
        print(f"Message is not JSON, raw body: {body}")