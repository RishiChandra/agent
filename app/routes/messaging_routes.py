"""
Messaging API routes for the mobile app.
Writes user messages to the messages database.
"""
import os
import sys
import traceback
import uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# Allow importing app modules when running as python main.py from app/ or as app.main from root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import execute_query, execute_update
from enqueue.message_enqueue import enqueue_text_message_safe

router = APIRouter()


@router.get("/messages")
def get_messages(chat_id: str):
    """
    Get all messages for a chat, sorted by created_at ascending.
    Returns list of {message_id, sender_id, content, created_at}.
    """
    print(f"[DEBUG] GET /messages chat_id={chat_id}")
    try:
        query = """
            SELECT message_id, sender_id, content, created_at
            FROM messages
            WHERE chat_id = %s::uuid
            ORDER BY created_at ASC
        """
        rows = execute_query(query, (chat_id,))
        print(f"[DEBUG] GET /messages chat_id={chat_id} count={len(rows)}")
        return {
            "messages": [
                {
                    "message_id": str(r["message_id"]),
                    "sender_id": str(r["sender_id"]),
                    "content": r["content"],
                    "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"]),
                }
                for r in rows
            ],
        }
    except Exception as e:
        print(f"[DEBUG] Error fetching messages chat_id={chat_id}: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching messages: {str(e)}")


class SendMessageRequest(BaseModel):
    """Request body for sending a message."""

    user_id: str
    chat_id: str
    content: str
    timestamp: str  # ISO 8601 datetime with timezone (e.g. "2025-02-01T12:00:00-08:00")


class EnqueueMessageRequest(BaseModel):
    """Request body for triggering message enqueue (AI/chip flow)."""

    user_id: str
    chat_id: str


@router.post("/messages/enqueue")
def enqueue_message(request: EnqueueMessageRequest):
    """
    Enqueue a text_message job so the AI will respond in the chip.
    Deduplicated per user (no-op if one is already pending).
    """
    print(f"[DEBUG] POST /messages/enqueue user_id={request.user_id} chat_id={request.chat_id}")
    result = enqueue_text_message_safe(request.user_id, request.chat_id)
    if result is None:
        print(f"[DEBUG] POST /messages/enqueue failed: result is None")
        raise HTTPException(status_code=500, detail="Enqueue failed")
    print(f"[DEBUG] POST /messages/enqueue result: {result}")
    return result


@router.post("/messages")
def send_message(request: SendMessageRequest):
    """
    Create a new message from the mobile app.

    Accepts user_id (stored as sender_id), chat_id, message content, and timestamp
    (stored as created_at). Auto-generates message_id (UUID). Composite PK: (chat_id, message_id).
    """
    print(f"[DEBUG] POST /messages user_id={request.user_id} chat_id={request.chat_id} content={request.content!r} timestamp={request.timestamp}")
    try:
        message_id = str(uuid.uuid4())
        ts = request.timestamp.strip()
        if not ts:
            raise HTTPException(status_code=400, detail="timestamp is required")

        query = """
            INSERT INTO messages (chat_id, message_id, sender_id, content, created_at, is_read)
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::timestamptz, false)
        """
        execute_update(
            query,
            (
                request.chat_id,
                message_id,
                request.user_id,
                request.content,
                ts,
            ),
        )
        print(f"[DEBUG] Message saved: message_id={message_id} chat_id={request.chat_id} user_id={request.user_id} content={request.content!r}")
        # Enqueue so the AI will respond in the chip (one pending text_message per user)
        enqueue_result = enqueue_text_message_safe(request.user_id, request.chat_id, message_id=message_id)
        print(f"[DEBUG] POST /messages enqueue_result: {enqueue_result}")
        return {
            "success": True,
            "message_id": message_id,
            "user_id": request.user_id,
            "chat_id": request.chat_id,
            "content": request.content,
            "created_at": request.timestamp,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[DEBUG] Error inserting message: {e}")
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Error saving message: {str(e)}",
        )
