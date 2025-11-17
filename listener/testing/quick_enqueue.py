#!/usr/bin/env python3
"""
Simple script to send a scheduled message to Azure Service Bus queue.

Usage:
    python quick_enqueue.py <minutes>
    (schedules message for specified minutes from now)
"""

import os
import sys
import json
from datetime import datetime, timedelta, UTC

from dotenv import load_dotenv

load_dotenv()


try:
    from azure.servicebus import ServiceBusClient, ServiceBusMessage
except ImportError:
    print("Error: azure-servicebus package not installed.")
    print("Install it with: pip install azure-servicebus")
    sys.exit(1)


def main():
    if len(sys.argv) != 2:
        print("Usage: python quick_enqueue.py <minutes>")
        print("Example: python quick_enqueue.py 5")
        sys.exit(1)
    
    message_contents = {
        "task_id": "a1f7c1c4-2b5c-4a19-9fc3-2d4b1a7c9e01",
        "title": "Morning workout",
        "description": "30-minute cardio session at the gym"
    }
    message_content = json.dumps(message_contents)
    
    try:
        minutes = int(sys.argv[1])
    except ValueError:
        print("Error: minutes must be a number")
        sys.exit(1)
    
    connection_string = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING")
    if not connection_string or not connection_string.strip():
        print("Error: AZURE_SERVICEBUS_CONNECTION_STRING environment variable not set or empty")
        print("Set it in your .env file or environment variables")
        sys.exit(1)
    
    connection_string = connection_string.strip()
    if not connection_string.startswith("Endpoint="):
        print("Error: Connection string appears to be malformed")
        print("Expected format: Endpoint=sb://...")
        sys.exit(1)
    
    scheduled_time = datetime.now(UTC) + timedelta(minutes=minutes)
    
    try:
        with ServiceBusClient.from_connection_string(connection_string) as client:
            with client.get_queue_sender("q1") as sender:
                message = ServiceBusMessage(message_content)
                sender.schedule_messages(message, scheduled_time)
                print(f"✅ Message scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
