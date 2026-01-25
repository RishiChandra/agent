import azure.functions as func
import datetime
import json
import logging
import sys
from pathlib import Path
import os

from datetime import datetime, timedelta, UTC
from session_management_utils import get_session
from iot_hub_mqtt import send_to_device, DEFAULT_DEVICE_ID
from azure.servicebus import ServiceBusClient, ServiceBusMessage

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
        user_id = "4dd16650-c57a-44c4-b530-fc1c15d50e45"

        try:
            session = get_session(user_id)
            if session:
                print(f"FOUND SESSION FOR USER {user_id} Session: {session}")
                if session["is_active"] is True:
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
                    # Send command to ESP32 to start websocket when session is inactive
                    try:
                        payload = {
                            "command": "start_websocket",
                            "reason": "session_inactive",
                            "user_id": user_id,
                            "original_message": body,
                        }
                        send_to_device(DEFAULT_DEVICE_ID, payload)
                        print(f"🚀 Sent start_websocket command to {DEFAULT_DEVICE_ID}")
                    except Exception as e:
                        print(f"Error sending message to device: {e}")
            else:
                print(f"COULD NOT FIND SESSION FOR USER {user_id}")
        except Exception as e:
            print(f"Error querying tasks table: {e}")

    except json.JSONDecodeError:
        print(f"Message is not JSON, raw body: {body}")