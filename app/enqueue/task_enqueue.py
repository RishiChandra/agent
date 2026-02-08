"""
Task enqueue operations module.
Handles enqueueing tasks to Azure Service Bus.
"""
import os
import json
from datetime import datetime, UTC
from typing import Optional, Dict, Any
from dotenv import load_dotenv

# Load environment variables from .env file (for local development)
load_dotenv()

try:
    from azure.servicebus import ServiceBusClient, ServiceBusMessage
except ImportError:
    print("Warning: azure-servicebus package not installed. Task enqueue functions will not work.")
    ServiceBusClient = None
    ServiceBusMessage = None


def get_service_bus_client():
    """
    Get Azure Service Bus client from connection string.
    
    Returns:
        ServiceBusClient instance
        
    Raises:
        ValueError: If connection string is not configured or invalid
    """
    if ServiceBusClient is None:
        raise ValueError("Azure Service Bus client not available. Please install azure-servicebus package.")
    
    connection_string = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING")
    if not connection_string or not connection_string.strip():
        raise ValueError("AZURE_SERVICEBUS_CONNECTION_STRING environment variable not set or empty")
    
    connection_string = connection_string.strip()
    # Remove surrounding quotes if present (from Azure environment variable setting)
    if connection_string.startswith('"') and connection_string.endswith('"'):
        connection_string = connection_string[1:-1]
    if connection_string.startswith("'") and connection_string.endswith("'"):
        connection_string = connection_string[1:-1]
    print(f"🔍 DEBUG: Connection string: {connection_string}")
    if not connection_string.startswith("Endpoint="):
        raise ValueError(f"Connection string appears to be malformed. Expected format: Endpoint=sb://... Got: {connection_string[:100]}")
    return ServiceBusClient.from_connection_string(connection_string)


def prepare_message_contents(
    task_id: str,
    user_id: str,
    task_info: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Prepare message contents for Service Bus from task data.
    
    Args:
        task_id: The task ID
        user_id: The user ID
        task_info: Optional task information dictionary
        
    Returns:
        Dictionary with message contents formatted for Service Bus
    """
    message_contents = {
        "task_id": task_id,
        "user_id": user_id,
        "pending_task": True,
        "pending_message": False,
    }
    
    # Add task_info fields if available
    if task_info:
        # Extract title and description from task_info
        # Flutter app sends task_info as {"info": "user input"}
        if "info" in task_info:
            info_text = task_info.get("info", "")
            # Use first line or first 50 chars as title, rest as description
            if info_text:
                lines = info_text.split('\n', 1)
                message_contents["title"] = task_info.get("title", lines[0][:50] if lines[0] else "Task")
                message_contents["description"] = info_text
            else:
                message_contents["title"] = task_info.get("title", "Task")
                message_contents["description"] = ""
        elif "title" in task_info:
            # If title is explicitly provided
            message_contents["title"] = task_info.get("title", "Task")
            message_contents["description"] = task_info.get("description", task_info.get("info", ""))
        else:
            # If task_info has other structure, include it and set defaults
            message_contents.update(task_info)
            if "title" not in message_contents:
                message_contents["title"] = "Task"
            if "description" not in message_contents:
                message_contents["description"] = ""
    
    return message_contents


def enqueue_task(
    task_id: str,
    user_id: str,
    task_info: Optional[Dict[str, Any]] = None,
    time_to_execute: Optional[str] = None,
    queue_name: str = "q1"
) -> Dict[str, Any]:
    """
    Enqueue a task to Azure Service Bus queue.
    
    Args:
        task_id: The task ID
        user_id: The user ID
        task_info: Optional task information dictionary
        time_to_execute: Optional ISO 8601 datetime string for scheduled delivery
        queue_name: Name of the Service Bus queue (default: "q1")
        
    Returns:
        Dictionary with success status and scheduling information
        
    Raises:
        ValueError: If Service Bus is not configured or connection fails
    """
    try:
        print(f"Enqueueing task {task_id} for user {user_id} with task info {task_info} and time to execute {time_to_execute}")
        # Prepare message contents
        message_contents = prepare_message_contents(task_id, user_id, task_info)
        message_content = json.dumps(message_contents)
        
        # Determine scheduled time
        scheduled_time = None
        if time_to_execute:
            try:
                # Parse ISO 8601 datetime string
                scheduled_time = datetime.fromisoformat(time_to_execute.replace('Z', '+00:00'))
                # Ensure it's timezone-aware (UTC)
                if scheduled_time.tzinfo is None:
                    scheduled_time = scheduled_time.replace(tzinfo=UTC)
                else:
                    scheduled_time = scheduled_time.astimezone(UTC)
            except ValueError as e:
                raise ValueError(f"Invalid time_to_execute format: {e}. Expected ISO 8601 format.")
        
        # Get Service Bus client and enqueue
        with get_service_bus_client() as client:
            with client.get_queue_sender(queue_name) as sender:
                message = ServiceBusMessage(message_content)
                
                if scheduled_time:
                    # Schedule the message for future delivery
                    sender.schedule_messages(message, scheduled_time)
                    print(f"✅ Task {task_id} scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                    return {
                        "success": True,
                        "task_id": task_id,
                        "scheduled_time": scheduled_time.isoformat(),
                        "message": f"Task scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                    }
                else:
                    # Send immediately
                    sender.send_messages(message)
                    print(f"✅ Task {task_id} enqueued immediately")
                    return {
                        "success": True,
                        "task_id": task_id,
                        "scheduled_time": None,
                        "message": "Task enqueued immediately"
                    }
                    
    except Exception as e:
        print(f"Error enqueueing task: {e}")
        raise


def enqueue_task_safe(
    task_id: str,
    user_id: str,
    task_info: Optional[Dict[str, Any]] = None,
    time_to_execute: Optional[str] = None,
    queue_name: str = "q1"
) -> Optional[Dict[str, Any]]:
    """
    Safely enqueue a task to Azure Service Bus queue.
    
    This is a non-raising version that returns None on error instead of raising exceptions.
    Useful for operations where enqueueing is optional and shouldn't fail the main operation.
    
    Args:
        task_id: The task ID
        user_id: The user ID
        task_info: Optional task information dictionary
        time_to_execute: Optional ISO 8601 datetime string for scheduled delivery
        queue_name: Name of the Service Bus queue (default: "q1")
        
    Returns:
        Dictionary with success status and scheduling information, or None if enqueueing failed
    """
    try:
        return enqueue_task(task_id, user_id, task_info, time_to_execute, queue_name)
    except Exception as e:
        print(f"Warning: Failed to enqueue task {task_id} to Service Bus: {e}")
        return None

