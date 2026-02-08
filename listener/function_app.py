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
from database import execute_query

app = func.FunctionApp()
connection_string = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING")


def get_unread_messages_for_chat(chat_id: str):
    """
    Fetch unread messages (is_read = false or null) for the given chat.
    Returns list of dicts with content, created_at.
    Marking as read is done only on the websocket side after the AI has been told.
    """
    query = """
        SELECT content, created_at
        FROM messages
        WHERE chat_id = %s::uuid
          AND (is_read IS FALSE OR is_read IS NULL)
        ORDER BY created_at ASC
    """
    return execute_query(query, (chat_id,))


@app.service_bus_queue_trigger(arg_name="msg",
                                queue_name="q1",
                                connection="AzureWebJobsServiceBus")
def QueueWorker(msg: func.ServiceBusMessage):
    print('Python HTTP trigger function processed a request.')
    body = msg.get_body().decode("utf-8")
    print(f"Received message: {body}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print(f"Message is not JSON, raw body: {body}")
        return

    message_type = data.get("message_type")
    user_id = data.get("user_id") or "4dd16650-c57a-44c4-b530-fc1c15d50e45"
    chat_id = data.get("chat_id")

    # Run session check for all messages (including text_message)
    try:
        session = get_session(user_id)
        if session:
            print(f"FOUND SESSION FOR USER {user_id} Session: {session}")
            if session["is_active"] is True:
                print(f"SESSION IS ACTIVE FOR USER {user_id}")
                scheduled_time = datetime.now(UTC) + timedelta(minutes=1)
                try:
                    with ServiceBusClient.from_connection_string(connection_string) as client:
                        with client.get_queue_sender("q1") as sender:
                            message = ServiceBusMessage(body)
                            sender.schedule_messages(message, scheduled_time)
                            print(f"✅ Message deferred for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                except Exception as e:
                    print(f"Error: {e}")
                    sys.exit(1)
                return  # Deferred; nothing more to do
            else:
                try:
                    payload = {
                        "command": "start_websocket",
                        "reason": "session_inactive",
                        "user_id": user_id,
                        "system_message": body,
                    }
                    send_to_device(DEFAULT_DEVICE_ID, payload)
                    print(f"🚀 Sent start_websocket command to {DEFAULT_DEVICE_ID}")
                except Exception as e:
                    print(f"Error sending message to device: {e}")
        else:
            print(f"COULD NOT FIND SESSION FOR USER {user_id}")
    except Exception as e:
        print(f"Error querying tasks table: {e}")

    # For type "text_message": send to chip via MQTT with user_id and type "text message"
    if message_type == "text_message":
        try:
            if not chat_id:
                print("text_message missing chat_id, skipping")
                return
            rows = get_unread_messages_for_chat(chat_id)
            text_parts = [r["content"] for r in rows] if rows else []
            text = "\n".join(text_parts) if text_parts else ""
            payload = {
                "command": "start_websocket",
                "reason": "text_message",
                "user_id": user_id,
                "pending_messages": True,
            }
            send_to_device(DEFAULT_DEVICE_ID, payload)
            print(f"Sent text_message to chip for user {user_id} (user_id, type 'text message') ({len(rows)} unread)")
            # is_read is marked only on the websocket side after the AI has been told about the messages
        except Exception as e:
            print(f"Error processing text_message: {e}")
        # pending_text_message_jobs is cleared only in the websocket after messages are retrieved by the SELECT