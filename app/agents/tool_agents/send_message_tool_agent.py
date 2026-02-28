"""
Send message tool: lets the user send a message to their caretaker or to their patient.
Supports both: patient → caretaker (UID2 = user, UID1 = recipient) and caretaker → patient (UID1 = user, UID2 = recipient).
Looks up relationship, finds chat_id from chat_members, then inserts into messages.
"""
import json
import uuid
from datetime import datetime, timezone

from database import execute_query, execute_update
from ..gemini_client import call_gemini, gemini_response_to_openai_like

try:
    from enqueue.message_enqueue import enqueue_text_message_safe
except ImportError:
    enqueue_text_message_safe = None


class SendMessageToolAgent:
    name = "send_message_tool"
    description = (
        "Send a message from the user to their caretaker or to their patient. "
        "Use when the user wants to send a message, text, or note to their caretaker (e.g. 'send X to my caretaker') "
        "or to their patient (e.g. 'send a message to my patient that...'). "
        "Extract the exact message content the user wants to send from the conversation."
    )

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history, user_config=None):
        # Resolve patient user_id
        user_id = None
        if user_config and user_config.get("user_info"):
            user_id = user_config["user_info"].get("user_id")
        if not user_id:
            return json.dumps({
                "success": False,
                "error": "user_id not available; cannot send message.",
            })

        # Extract message content from chat via LLM
        most_recent_user_message = None
        for msg in reversed(chat_history):
            if msg.get("role") == "user":
                most_recent_user_message = msg.get("content", "")
                break

        system_content = (
            f"Given the chat history {chat_history}, the assistant has decided to use the {self.name}. "
            "Extract the exact message content the user wants to send (to their caretaker or to their patient) from the most recent user message. "
            "Return only the message text the user wants to send; do not add greetings or extra wording."
        )
        if most_recent_user_message:
            system_content += f'\n\nThe most recent user message is: "{most_recent_user_message}"'

        selecting_tool = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The exact message text the user wants to send (to caretaker or patient).",
                        },
                    },
                    "required": ["content"],
                },
            },
        }
        messages = [{"role": "system", "content": system_content}]
        response = gemini_response_to_openai_like(call_gemini(messages, [selecting_tool]))
        tool_calls = getattr(response.choices[0].message, "tool_calls", None) if response.choices else None
        if not tool_calls or len(tool_calls) == 0:
            return json.dumps({
                "success": False,
                "error": "Could not extract message content from the user's request (model did not return a tool call).",
            })
        arguments = json.loads(tool_calls[0].function.arguments)
        content = (arguments.get("content") or "").strip()
        if not content:
            return json.dumps({
                "success": False,
                "error": "No message content could be extracted from the user's request.",
            })

        # 1) Relationships: find the other party (recipient). Support both directions:
        #    - User is patient (UID2): recipient is UID1 (caretaker)
        #    - User is caretaker (UID1): recipient is UID2 (patient)
        try:
            # First try: user is patient (UID2), recipient is caretaker (UID1)
            rel_rows = execute_query(
                "SELECT UID1 AS recipient_id FROM relationships WHERE UID2 = %s::uuid AND rel_type = %s LIMIT 1",
                (user_id, "caretaker_patient"),
            )
            if rel_rows:
                recipient_id = str(rel_rows[0]["recipient_id"])
            else:
                # Second try: user is caretaker (UID1), recipient is patient (UID2)
                rel_rows = execute_query(
                    "SELECT UID2 AS recipient_id FROM relationships WHERE UID1 = %s::uuid AND rel_type = %s LIMIT 1",
                    (user_id, "caretaker_patient"),
                )
                if not rel_rows:
                    return json.dumps({
                        "success": False,
                        "error": "No caretaker–patient relationship found for this user.",
                    })
                recipient_id = str(rel_rows[0]["recipient_id"])
        except Exception as e:
            print(f"Error querying relationships: {e}")
            return json.dumps({"success": False, "error": "Failed to look up relationship."})

        # 2) Chat members: find chat_id that contains both user and recipient
        try:
            chat_rows = execute_query(
                """
                SELECT cm1.chat_id
                FROM chat_members cm1
                JOIN chat_members cm2 ON cm1.chat_id = cm2.chat_id
                WHERE cm1.user_id = %s::uuid AND cm2.user_id = %s::uuid
                LIMIT 1
                """,
                (user_id, recipient_id),
            )
        except Exception as e:
            print(f"Error querying chat_members: {e}")
            return json.dumps({"success": False, "error": "Failed to find chat with recipient."})
        if not chat_rows:
            return json.dumps({
                "success": False,
                "error": "No chat found between you and the recipient.",
            })
        chat_id = str(chat_rows[0]["chat_id"])

        # 3) Insert into messages
        message_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            execute_update(
                """
                INSERT INTO messages (chat_id, message_id, sender_id, content, created_at, is_read)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::timestamptz, false)
                """,
                (chat_id, message_id, user_id, content, created_at),
            )
        except Exception as e:
            print(f"Error inserting message: {e}")
            return json.dumps({"success": False, "error": "Failed to save message."})

        # Notify recipient (optional; enqueue so their chip can show the new message)
        if enqueue_text_message_safe:
            enqueue_text_message_safe(recipient_id, chat_id, message_id=message_id)

        return json.dumps({
            "success": True,
            "message_id": message_id,
            "chat_id": chat_id,
            "content": content,
        })
