"""
Message enqueue operations for the AI/chip flow.
Enqueues "text_message" jobs to Azure Service Bus so the listener will
fetch unread messages and send them to the chip via MQTT.
Deduplicates by user: at most one pending text_message job per user.
Messages are scheduled for 1 minute later (not immediately).
"""
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import sys
# Allow importing app modules when running from app/ or as app from root
_app_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _app_path not in sys.path:
    sys.path.insert(0, _app_path)

from database import execute_query, execute_update

try:
    from enqueue.task_enqueue import get_service_bus_client
    from azure.servicebus import ServiceBusMessage
except ImportError:
    get_service_bus_client = None
    ServiceBusMessage = None

# Queue used by the listener (same as task queue)
MESSAGE_QUEUE_NAME = "q1"

# Table used to ensure only one pending text_message job per user.
# Schema: (user_id uuid, message_id uuid, ...) with composite unique/primary key (user_id, message_id).
# Other code (e.g. websocket_handler) JOINs on message_id, so message_id is required.
PENDING_TABLE = "pending_text_message_jobs"

# Sentinel UUID when we claim a slot without a specific message (e.g. POST /messages/enqueue).
CLAIM_SENTINEL_MESSAGE_ID = "00000000-0000-0000-0000-000000000000"


def _has_pending_text_message_job(user_id: str) -> bool:
    """
    Check if there is any entry in pending_text_message_jobs for this user_id.
    Returns True if at least one row exists, False if no entries.
    """
    try:
        rows = execute_query(
            "SELECT 1 FROM pending_text_message_jobs WHERE user_id = %s::uuid LIMIT 1",
            (user_id,),
        )
        has_pending = len(rows) > 0
        print(f"[DEBUG] _has_pending_text_message_job user_id={user_id} -> {has_pending}")
        return has_pending
    except Exception as e:
        print(f"Warning: pending_text_message_jobs check failed (table may not exist): {e}")
        return False  # allow enqueue if table missing (degraded mode)


def _try_claim_pending_text_message_job(user_id: str, message_id: Optional[str] = None) -> bool:
    """
    Try to claim a pending text_message slot for this user.
    Inserts (user_id, message_id) only when no row exists for this user_id (avoids ON CONFLICT
    so we do not require a unique constraint on user_id alone; table may have (user_id, message_id) composite key).
    Returns True if we inserted (no existing pending), False if already pending.
    """
    message_id = message_id or CLAIM_SENTINEL_MESSAGE_ID
    try:
        query = """
            INSERT INTO pending_text_message_jobs (user_id, message_id)
            SELECT %s::uuid, %s::uuid
            WHERE NOT EXISTS (
                SELECT 1 FROM pending_text_message_jobs WHERE user_id = %s::uuid
            )
        """
        n = execute_update(query, (user_id, message_id, user_id))
        claimed = n > 0
        print(f"[DEBUG] _try_claim_pending_text_message_job user_id={user_id} rows_affected={n} claimed={claimed}")
        return claimed
    except Exception as e:
        print(f"Warning: pending_text_message_jobs insert failed (table may not exist): {e}")
        return True  # allow enqueue if table missing (degraded mode)


def _clear_pending_text_message_job(user_id: str) -> None:
    """Clear the pending text_message slot for this user (call from listener after processing)."""
    try:
        query = "DELETE FROM pending_text_message_jobs WHERE user_id = %s::uuid"
        n = execute_update(query, (user_id,))
        print(f"[DEBUG] _clear_pending_text_message_job user_id={user_id} rows_deleted={n}")
    except Exception as e:
        print(f"Warning: failed to clear pending_text_message_job for {user_id}: {e}")


def enqueue_text_message(
    user_id: str,
    chat_id: str,
    message_id: Optional[str] = None,
    queue_name: str = MESSAGE_QUEUE_NAME,
) -> Dict[str, Any]:
    """
    Enqueue a text_message job for the listener so the AI will respond in the chip.
    Check first: if any message is already pending for this user_id, do not enqueue.
    If no message is pending, claim a slot (insert) and enqueue.
    Message is scheduled for 1 minute later.

    Args:
        user_id: User who sent the message.
        chat_id: Chat the message belongs to.
        message_id: Optional message UUID (included in payload when provided).
        queue_name: Service Bus queue name (default: q1).

    Returns:
        Dict with success, message, and optionally enqueued=True/False.
    """
    # Single source of truth: try to claim a slot (INSERT only when no row for this user).
    # Do not rely on a separate SELECT — it can see different state (e.g. different DB than pgAdmin, replication lag).
    if not _try_claim_pending_text_message_job(user_id, message_id):
        print(f"[DEBUG] enqueue_text_message user_id={user_id} chat_id={chat_id} skipped (claim failed, already pending)")
        return {
            "success": True,
            "enqueued": False,
            "message": "Text message job already pending for this user; skipped duplicate.",
        }

    if get_service_bus_client is None or ServiceBusMessage is None:
        print("Warning: Azure Service Bus not available; text message not enqueued.")
        return {"success": False, "enqueued": False, "message": "Service Bus not available."}

    payload = {
        "message_type": "text_message",
        "user_id": user_id,
        "chat_id": chat_id,
        "pending_task": False,
        "pending_message": True,
    }
    if message_id is not None:
        payload["message_id"] = message_id
    body = json.dumps(payload)
    scheduled_time = datetime.now(timezone.utc) + timedelta(minutes=1)
    print(f"[DEBUG] enqueue_text_message user_id={user_id} chat_id={chat_id} message_id={message_id} scheduling at {scheduled_time.isoformat()}")

    try:
        with get_service_bus_client() as client:
            with client.get_queue_sender(queue_name) as sender:
                message = ServiceBusMessage(body, scheduled_enqueue_time_utc=scheduled_time)
                sender.send_messages(message)
        print(f"[DEBUG] enqueue_text_message user_id={user_id} chat_id={chat_id} enqueued to Service Bus (scheduled)")
        return {
            "success": True,
            "enqueued": True,
            "message": "Text message job enqueued.",
        }
    except Exception as e:
        print(f"[DEBUG] Error enqueueing text_message user_id={user_id} chat_id={chat_id}: {e}")
        _clear_pending_text_message_job(user_id)  # release claim so they can retry
        return {
            "success": False,
            "enqueued": False,
            "message": str(e),
        }


def enqueue_text_message_safe(
    user_id: str,
    chat_id: str,
    message_id: Optional[str] = None,
    queue_name: str = MESSAGE_QUEUE_NAME,
) -> Optional[Dict[str, Any]]:
    """
    Safe (non-raising) enqueue of a text_message job.
    Returns the result dict or None on unexpected error.
    """
    try:
        return enqueue_text_message(user_id, chat_id, message_id, queue_name)
    except Exception as e:
        print(f"[DEBUG] enqueue_text_message_safe failed user_id={user_id} chat_id={chat_id}: {e}")
        return None


# Expose for listener to clear pending after processing
def clear_pending_text_message_job(user_id: str) -> None:
    _clear_pending_text_message_job(user_id)
