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

router = APIRouter()


@router.get("/messages")
def get_messages(chat_id: str):
    """
    Get all messages for a chat, sorted by created_at ascending.
    Returns list of {message_id, sender_id, content, created_at}.
    """
    try:
        query = """
            SELECT message_id, sender_id, content, created_at
            FROM messages
            WHERE chat_id = %s::uuid
            ORDER BY created_at ASC
        """
        rows = execute_query(query, (chat_id,))
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
        print(f"Error fetching messages: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error fetching messages: {str(e)}")


class SendMessageRequest(BaseModel):
    """Request body for sending a message."""

    user_id: str
    chat_id: str
    content: str
    timestamp: str  # ISO 8601 datetime with timezone (e.g. "2025-02-01T12:00:00-08:00")


@router.post("/messages")
def send_message(request: SendMessageRequest):
    """
    Create a new message from the mobile app.

    Accepts user_id (stored as sender_id), chat_id, message content, and timestamp
    (stored as created_at). Auto-generates message_id (UUID). Composite PK: (chat_id, message_id).
    """
    try:
        message_id = str(uuid.uuid4())
        ts = request.timestamp.strip()
        if not ts:
            raise HTTPException(status_code=400, detail="timestamp is required")

        query = """
            INSERT INTO messages (chat_id, message_id, sender_id, content, created_at)
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::timestamptz)
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
        print(f"Error inserting message: {e}")
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Error saving message: {str(e)}",
        )
