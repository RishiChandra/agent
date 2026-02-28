"""
Edit-task enqueue operations module.
Cancels the existing scheduled message (by sequence_id from the task table) and
re-enqueues a new message with updated time / info / payload using the same format as task_enqueue.
"""
from typing import Optional, Dict, Any

from database import execute_query, execute_update
from enqueue.task_enqueue import get_service_bus_client, enqueue_task


def reenqueue_task_after_edit(
    task_id: str,
    user_id: str,
    task_info: Optional[Dict[str, Any]] = None,
    time_to_execute: Optional[str] = None,
    queue_name: str = "q1",
) -> Dict[str, Any]:
    """
    Look up the task's enqueue_sequence_id in the SQL table, cancel that scheduled
    message in Service Bus, then enqueue a new message with the updated time/info/payload.

    Uses the same message format as task_enqueue (prepare_message_contents, enqueue_task).

    Args:
        task_id: The task ID
        user_id: The user ID (must match task row)
        task_info: Updated task information dict (e.g. {"info": "description"})
        time_to_execute: Updated ISO 8601 datetime string for scheduled delivery
        queue_name: Service Bus queue name (default: "q1")

    Returns:
        Enqueue result dict from enqueue_task (success, task_id, scheduled_time, sequence_id, ...)

    Raises:
        ValueError: If task not found or Service Bus not configured
    """
    # Load current enqueue_sequence_id from tasks table
    query = """
        SELECT enqueue_sequence_id
        FROM tasks
        WHERE task_id = %s AND user_id = %s
    """
    rows = execute_query(query, (task_id, user_id))
    if not rows:
        raise ValueError(f"Task not found: task_id={task_id}, user_id={user_id}")

    enqueue_sequence_id = rows[0].get("enqueue_sequence_id") if rows else None

    # Cancel existing scheduled message if we have a sequence number
    if enqueue_sequence_id is not None:
        with get_service_bus_client() as client:
            with client.get_queue_sender(queue_name) as sender:
                sender.cancel_scheduled_messages(enqueue_sequence_id)
                print(f"✅ Cancelled scheduled message for task {task_id} (sequence_id={enqueue_sequence_id})")

    # Enqueue new message with updated payload (same format as task_enqueue)
    result = enqueue_task(
        task_id=task_id,
        user_id=user_id,
        task_info=task_info,
        time_to_execute=time_to_execute,
        queue_name=queue_name,
    )

    # Persist new sequence_id to task row when present (scheduled messages only)
    if result.get("sequence_id") is not None:
        try:
            execute_update(
                "UPDATE tasks SET enqueue_sequence_id = %s WHERE task_id = %s",
                (result["sequence_id"], task_id),
            )
        except Exception as update_err:
            print(f"Warning: Failed to update enqueue_sequence_id for task {task_id}: {update_err}")

    return result


def cancel_scheduled_task_for_task_id(
    task_id: str,
    user_id: str,
    queue_name: str = "q1",
) -> bool:
    """
    Look up the task's enqueue_sequence_id and cancel that scheduled message in Service Bus.
    Also sets enqueue_sequence_id to NULL in the task row. Use when e.g. task is marked completed.

    Returns:
        True if a message was cancelled (or no sequence_id was stored), False if task not found.
    """
    query = """
        SELECT enqueue_sequence_id
        FROM tasks
        WHERE task_id = %s AND user_id = %s
    """
    rows = execute_query(query, (task_id, user_id))
    if not rows:
        return False

    enqueue_sequence_id = rows[0].get("enqueue_sequence_id") if rows else None
    if enqueue_sequence_id is None:
        return True  # Nothing to cancel

    with get_service_bus_client() as client:
        with client.get_queue_sender(queue_name) as sender:
            sender.cancel_scheduled_messages(enqueue_sequence_id)
            print(f"✅ Cancelled scheduled message for task {task_id} (sequence_id={enqueue_sequence_id})")

    execute_update(
        "UPDATE tasks SET enqueue_sequence_id = NULL WHERE task_id = %s",
        (task_id,),
    )
    return True


def cancel_scheduled_task_for_task_id_safe(
    task_id: str,
    user_id: str,
    queue_name: str = "q1",
) -> bool:
    """Non-raising version of cancel_scheduled_task_for_task_id. Returns False on error."""
    try:
        return cancel_scheduled_task_for_task_id(task_id, user_id, queue_name)
    except Exception as e:
        print(f"Warning: Failed to cancel scheduled message for task {task_id}: {e}")
        return False


def reenqueue_task_after_edit_safe(
    task_id: str,
    user_id: str,
    task_info: Optional[Dict[str, Any]] = None,
    time_to_execute: Optional[str] = None,
    queue_name: str = "q1",
) -> Optional[Dict[str, Any]]:
    """
    Non-raising version of reenqueue_task_after_edit. Returns None on error.
    """
    try:
        return reenqueue_task_after_edit(
            task_id=task_id,
            user_id=user_id,
            task_info=task_info,
            time_to_execute=time_to_execute,
            queue_name=queue_name,
        )
    except Exception as e:
        print(f"Warning: Failed to re-enqueue task {task_id} after edit: {e}")
        return None
