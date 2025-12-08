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
                    # Start websocket connection when session is inactive
                    return True
                    print(f"SESSION IS INACTIVE FOR USER {user_id}")
                    try:
                        # Extract message from body if it's JSON, otherwise use default
                        try:
                            message_data = json.loads(body)
                            reminder_message = message_data.get("message", "Remind me of my tasks today")
                        except (json.JSONDecodeError, AttributeError):
                            reminder_message = "Remind me of my tasks today"
                        
                        print(f"🚀 Starting websocket connection for user {user_id} with message: {reminder_message}")
                        start_websocket_connection(user_id, reminder_message)
                    except Exception as e:
                        print(f"❌ Error starting websocket connection: {e}")
                        # Don't exit, just log the error

           else:
                print(f"COULD NOT FIND SESSION FOR USER {user_id}")
        except Exception as e:
            print(f"Error querying tasks table: {e}")
            
    except json.JSONDecodeError:
        print(f"Message is not JSON, raw body: {body}")