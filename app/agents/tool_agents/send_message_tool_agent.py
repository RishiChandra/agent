import json
import uuid
from datetime import datetime, timezone

from database import execute_query, execute_update
from ..gemini_client import call_gemini, gemini_response_to_openai_like


class SendMessageToolAgent:
    name = "send_message_tool"
    description = (
        "Send a message to the user's caretaker. Use this tool when the user explicitly asks to send a message "
        "to their caretaker or implies replying to them (e.g., 'send a message to my caretaker', 'text my caretaker that...', "
        "'tell my caretaker...', 'tell her...', 'let him know...', 'reply to her...'). "
        "The tool finds the caretaker via the relationships table (user is patient, uid2) and writes the message to the messages table. "
        "Do NOT use for creating tasks or reminders; use create_tasks_tool for those."
    )

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history, user_config=None):
        """
        Send a message to the user's caretaker: extract message content from chat,
        resolve caretaker and chat_id via relationships + chat_members, then insert into messages.
        """
        user_name = user_config.get("user_name") if user_config else "the user"
        current_time_str = user_config.get("current_time_str") if user_config else "unknown time"
        current_date_str = user_config.get("current_date_str") if user_config else "unknown date"

        # Current user is the patient (uid2 in relationships); we send to caretaker (uid1)
        user_id = None
        if user_config and user_config.get("user_info"):
            user_id = user_config["user_info"].get("user_id")
        if not user_id:
            user_id = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
            print(f"Warning: user_id not found in user_config, using fallback: {user_id}")

        # Find the most recent user message
        most_recent_user_message = None
        for msg in reversed(chat_history):
            if msg.get("role") == "user":
                most_recent_user_message = msg.get("content", "")
                break

        system_content = (
            f"Given the chat history {chat_history}, the assistant has decided to use the {self.name}."
            f"\n\nUSER CONTEXT:\n- User name: {user_name}\n- Current time: {current_time_str}\n- Current date: {current_date_str}"
            "\n\nThe user is the PATIENT sending a message to their CARETAKER. Extract only the message body they want to send."
        )
        if most_recent_user_message:
            system_content += (
                f"\n\nThe MOST RECENT user message is: \"{most_recent_user_message}\""
                "\n- Extract the exact message body the user wants to send to their caretaker."
                "\n- Use the user's words; do not add content they did not ask for."
            )
        system_content += (
            "\n\nRULES:"
            "\n- message: The exact text to send to the caretaker. Required."
        )

        messages = [
            {"role": "system", "content": system_content},
        ]

        selecting_tool = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The exact message body to send to the user's caretaker. Use the user's wording.",
                        },
                    },
                    "required": ["message"],
                },
            },
        }

        response = gemini_response_to_openai_like(call_gemini(messages, [selecting_tool]))
        tool_calls = getattr(response.choices[0].message, "tool_calls", None)
        if not tool_calls or len(tool_calls) == 0:
            return json.dumps({
                "success": False,
                "message": "Could not extract message content from the request.",
            })

        arguments = json.loads(tool_calls[0].function.arguments)
        message_body = (arguments.get("message") or "").strip()

        if not message_body:
            return json.dumps({
                "success": False,
                "message": "Message content is required.",
            })

        try:
            # 1) Find caretaker: relationships where current user is patient (uid2), rel_type = caretaker_patient -> uid1
            rel_query = """
                SELECT uid1
                FROM relationships
                WHERE uid2 = %s::uuid AND rel_type = %s
            """
            rel_rows = execute_query(rel_query, (user_id, "caretaker_patient"))
            if not rel_rows:
                return json.dumps({
                    "success": False,
                    "message": "No caretaker relationship found for the current user (patient).",
                })
            caretaker_uid = str(rel_rows[0]["uid1"])

            # 2) Find chat_id that has BOTH patient and caretaker (AND logic via chat_members)
            chat_query = """
                SELECT chat_id
                FROM chat_members
                WHERE user_id IN (%s::uuid, %s::uuid)
                GROUP BY chat_id
                HAVING COUNT(DISTINCT user_id) = 2
            """
            chat_rows = execute_query(chat_query, (user_id, caretaker_uid))
            if not chat_rows:
                return json.dumps({
                    "success": False,
                    "message": "No chat found between you and your caretaker.",
                })
            chat_id = str(chat_rows[0]["chat_id"])

            # 3) Insert into messages: chat_id, message_id, sender_id (patient), content, created_at, is_read
            message_id = str(uuid.uuid4())
            created_at = datetime.now(timezone.utc)
            insert_query = """
                INSERT INTO messages (chat_id, message_id, sender_id, content, created_at, is_read)
                VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s::timestamptz, false)
            """
            execute_update(insert_query, (chat_id, message_id, user_id, message_body, created_at))

            print(f"[send_message_tool] chat_id={chat_id} message_id={message_id} sender_id={user_id} content={message_body[:80]!r}")

            return json.dumps({
                "success": True,
                "message": "Message sent to your caretaker.",
                "message_id": message_id,
                "chat_id": chat_id,
            })
        except Exception as e:
            print(f"Error in send_message_tool: {e}")
            return json.dumps({
                "success": False,
                "message": str(e),
            })
