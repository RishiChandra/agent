import azure.functions as func
import datetime
import json
import logging
import sys
from pathlib import Path
import os

from datetime import datetime, timedelta, UTC
from session_management_utils import get_session
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from mock_task_reminder import start_websocket_connection

app = func.FunctionApp()
connection_string = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING")
@app.service_bus_queue_trigger(arg_name="msg", 
                                queue_name="q1",
                                connection="AzureWebJobsServiceBus")
def QueueWorker(msg: func.ServiceBusMessage):
    print('Python HTTP trigger function processed a request.')
    body = msg.get_body().decode("utf-8")
    print(f"Received message: {body}")
    
    # Process the message body
    try:
        user_id = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
                
        try:
           session = get_session(user_id)
           if session:
                print(f"FOUND SESSION FOR USER {user_id} Session: {session}")
                if session["is_active"] == True:
                    print(f"SESSION IS ACTIVE FOR USER {user_id}")
                    print(f"Connection string: {connection_string}")
                    scheduled_time = datetime.now(UTC) + timedelta(minutes=1)
                    try:
                        with ServiceBusClient.from_connection_string(connection_string) as client:
                            with client.get_queue_sender("q1") as sender:
                                message = ServiceBusMessage(body)
                                sender.schedule_messages(message, scheduled_time)
                                print(f"✅ Message deffered for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                    except Exception as e:
                        print(f"Error: {e}")
                        sys.exit(1)
                else:
                    # LOGIC TO SEND NOTIFICATION THROUGH AZURE IOT HUB ON ESP32 TO START WEBSOCKET

           else:
                print(f"COULD NOT FIND SESSION FOR USER {user_id}")
        except Exception as e:
            print(f"Error querying tasks table: {e}")
            
    except json.JSONDecodeError:
        print(f"Message is not JSON, raw body: {body}")