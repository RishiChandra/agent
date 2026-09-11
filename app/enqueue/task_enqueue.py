"""
Task enqueue operations module.
Inserts scheduled jobs into the Postgres `jobs` table (deploy/sql/001_jobs.sql);
listener/worker.py picks them up and wakes the device over MQTT.
(Previously: Azure Service Bus queue "q1".)

The job id is returned as `sequence_id` and stored in tasks.enqueue_sequence_id,
so edit/cancel flows can find the pending job later.
"""
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from psycopg2.extras import Json

from database import get_db_connection, execute_update

# Load environment variables from .env file (for local development)
load_dotenv()


def insert_job(kind: str, payload: Dict[str, Any], deliver_at: Optional[datetime] = None) -> int:
    """
    Insert one row into `jobs` and return its id.

    Args:
        kind: 'task' or 'text_message'
        payload: JSON body the worker will process (same shape as the old queue message)
        deliver_at: earliest delivery time (timezone-aware); None = now
    """
    conn = get_db_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO jobs (kind, payload, deliver_at)
                    VALUES (%s, %s, COALESCE(%s, now()))
                    RETURNING id
                    """,
                    (kind, Json(payload), deliver_at),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


def cancel_job(job_id: int) -> bool:
    """
    Remove a job that has not run yet. Returns True if a pending row was deleted,
    False if the job was already delivered (or never existed).
    """
    return execute_update("DELETE FROM jobs WHERE id = %s AND done_at IS NULL", (job_id,)) > 0


def prepare_message_contents(
    task_id: str,
    user_id: str,
    task_info: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Prepare the job payload from task data.
    
    Args:
        task_id: The task ID
        user_id: The user ID
        task_info: Optional task information dictionary
        
    Returns:
        Dictionary with message contents the worker/device expect
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
    Enqueue a task job for the worker.
    
    Args:
        task_id: The task ID
        user_id: The user ID
        task_info: Optional task information dictionary
        time_to_execute: Optional ISO 8601 datetime string for scheduled delivery
        queue_name: Unused; kept so existing call sites keep working
        
    Returns:
        Dictionary with success status, scheduling information and sequence_id
        (the jobs.id, persisted by callers into tasks.enqueue_sequence_id).
        
    Raises:
        ValueError: If time_to_execute is malformed
        psycopg2.Error: If the insert fails
    """
    try:
        print(f"Enqueueing task {task_id} for user {user_id} with task info {task_info} and time to execute {time_to_execute}")
        message_contents = prepare_message_contents(task_id, user_id, task_info)
        
        # Determine scheduled time
        scheduled_time = None
        if time_to_execute:
            try:
                # Parse ISO 8601 datetime string
                scheduled_time = datetime.fromisoformat(time_to_execute.replace('Z', '+00:00'))
                # Ensure it's timezone-aware (UTC)
                if scheduled_time.tzinfo is None:
                    scheduled_time = scheduled_time.replace(tzinfo=timezone.utc)
                else:
                    scheduled_time = scheduled_time.astimezone(timezone.utc)
            except ValueError as e:
                raise ValueError(f"Invalid time_to_execute format: {e}. Expected ISO 8601 format.")
        
        job_id = insert_job("task", message_contents, scheduled_time)

        if scheduled_time:
            print(f"✅ Task {task_id} scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')} (job_id={job_id})")
            return {
                "success": True,
                "task_id": task_id,
                "scheduled_time": scheduled_time.isoformat(),
                "message": f"Task scheduled for {scheduled_time.strftime('%Y-%m-%d %H:%M:%S UTC')}",
                "sequence_id": job_id,
            }
        print(f"✅ Task {task_id} enqueued immediately (job_id={job_id})")
        return {
            "success": True,
            "task_id": task_id,
            "scheduled_time": None,
            "message": "Task enqueued immediately",
            "sequence_id": job_id,
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
    Safely enqueue a task job.
    
    This is a non-raising version that returns None on error instead of raising exceptions.
    Useful for operations where enqueueing is optional and shouldn't fail the main operation.
    
    Args:
        task_id: The task ID
        user_id: The user ID
        task_info: Optional task information dictionary
        time_to_execute: Optional ISO 8601 datetime string for scheduled delivery
        queue_name: Unused; kept so existing call sites keep working
        
    Returns:
        Dictionary with success status and scheduling information, or None if enqueueing failed
    """
    try:
        return enqueue_task(task_id, user_id, task_info, time_to_execute, queue_name)
    except Exception as e:
        print(f"Warning: Failed to enqueue task {task_id}: {e}")
        return None
