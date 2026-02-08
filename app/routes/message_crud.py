"""
Message CRUD operations module.
Handles pending message jobs and marking messages as read.
"""
from typing import List, Dict, Any

from database import execute_query, execute_update


def get_pending_messages_for_user(user_id: str) -> List[Dict[str, Any]]:
    """
    Read pending_text_message_jobs (user_id, message_id) for this user, then get
    message content and sender name from messages and users. Only includes
    messages where is_read is false or null. Sort by created_at.

    Returns list of dicts with keys: chat_id, message_id, content, created_at, sender_name.
    """
    query = """
        SELECT m.chat_id, m.message_id, m.content, m.created_at, u.first_name
        FROM pending_text_message_jobs p
        JOIN messages m ON m.message_id = p.message_id
        LEFT JOIN users u ON u.user_id = m.sender_id
        WHERE p.user_id = %s::uuid
          AND (m.is_read IS FALSE OR m.is_read IS NULL)
        ORDER BY m.created_at ASC
    """
    try:
        rows = execute_query(query, (user_id,))
        return [
            {
                "chat_id": r.get("chat_id"),
                "message_id": r.get("message_id"),
                "content": r.get("content") or "",
                "created_at": r.get("created_at"),
                "sender_name": r.get("first_name") or "Unknown",
            }
            for r in rows
        ]
    except Exception as e:
        print(f"Warning: get_pending_messages_for_user failed for {user_id}: {e}")
        return []


def mark_messages_as_read(entries: List[Dict[str, Any]]) -> None:
    """
    Set is_read = true for each message in entries (each must have chat_id and message_id).
    Call after the websocket server has "read" the messages (e.g. sent them to the AI).
    """
    for entry in entries:
        chat_id = entry.get("chat_id")
        message_id = entry.get("message_id")
        if not chat_id or not message_id:
            continue
        try:
            execute_update(
                "UPDATE messages SET is_read = true WHERE chat_id = %s::uuid AND message_id = %s::uuid",
                (chat_id, message_id),
            )
        except Exception as e:
            print(f"Warning: failed to mark message {message_id} as read: {e}")


def clear_pending_text_message_job_for_user(user_id: str) -> None:
    """
    Delete this user's row from pending_text_message_jobs.
    Call only after messages have been retrieved (by get_pending_messages_for_user) and processed.
    """
    try:
        execute_update(
            "DELETE FROM pending_text_message_jobs WHERE user_id = %s::uuid",
            (user_id,),
        )
    except Exception as e:
        print(f"Warning: failed to clear pending_text_message_job for user {user_id}: {e}")
