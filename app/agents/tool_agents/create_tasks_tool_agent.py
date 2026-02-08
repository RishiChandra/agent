import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from database import execute_update
from psycopg2.extras import Json

from ..openai_client import call_openai
from enqueue.task_enqueue import enqueue_task

class CreateTasksToolAgent:
    name = "create_tasks_tool"
    description = "Create a new task with a description and time to execute. Use this tool ONLY when the user explicitly asks to CREATE, SCHEDULE, SET, or ADD a task. NEVER use this tool for read-only queries like 'What tasks do I have' or 'Show me my tasks'. Use this tool at most once for each user instruction unless the user explicitly asks for multiple tasks."

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history, user_config=None):
        # Build system message with user config context for time parsing
        # Pull user context (as provided by main.py)
        user_name = user_config.get("user_name") if user_config else "the user"
        current_time_str = user_config.get("current_time_str") if user_config else "unknown time"
        current_date_str = user_config.get("current_date_str") if user_config else "unknown date"
        timezone = user_config.get("timezone") if user_config else "UTC"

        # Find the most recent user message
        most_recent_user_message = None
        for msg in reversed(chat_history):
            if msg.get("role") == "user":
                most_recent_user_message = msg.get("content", "")
                break
        
        # Check chat history for already-created tasks (only count those created AFTER the most recent user message)
        created_tasks = []
        found_most_recent_user = False
        for msg in chat_history:
            if msg.get("role") == "user" and msg.get("content") == most_recent_user_message:
                found_most_recent_user = True
                continue
            if found_most_recent_user and msg.get("name") == "create_tasks_tool" and msg.get("content"):
                try:
                    content = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                    if content.get("success") and content.get("task_info"):
                        task_info = content.get("task_info", {})
                        task_desc = task_info.get("info", "") if isinstance(task_info, dict) else str(task_info)
                        created_tasks.append(task_desc.lower().strip())
                except:
                    pass
        
        system_content = (
            f"Given the chat history {chat_history}, the assistant has decided to use the {self.name}."
            f"\n\nUSER CONTEXT:\n- User name: {user_name}\n- Current time: {current_time_str}\n- Current date: {current_date_str}\n- User timezone: {timezone}"
        )
        
        if most_recent_user_message:
            system_content += (
                f"\n\n⚠️ CRITICAL: The MOST RECENT user message is: \"{most_recent_user_message}\""
                f"\n- You MUST ONLY extract tasks from this message. Do NOT extract tasks from previous messages or make up tasks."
                f"\n- If the user said 'brush my teeth at 6am and eat breakfast at 11am', extract ONLY 'brush my teeth' or 'eat breakfast' - nothing else."
                f"\n\n⚠️ TIME INTERPRETATION FOR THIS REQUEST:"
                f"\n- Current date is: {current_date_str}"
                f"\n- If the user says 'tonight', 'today', 'this evening', etc., or if no relative phrase is provided, they mean {current_date_str} - NOT tomorrow."
                f"\n- ONLY use tomorrow's date if the user explicitly says 'tomorrow'."
            )
        
        if created_tasks:
            system_content += (
                f"\n\n⚠️ CRITICAL: The following tasks have ALREADY been created from the most recent user message: {created_tasks}"
                f"\n- You MUST extract a DIFFERENT task from the MOST RECENT user message that has NOT been created yet."
                f"\n- If all tasks from the most recent user message have been created, return an error."
            )
        
        system_content += (
            "\n\nTASK EXTRACTION RULES:"
            "\n- Extract tasks ONLY from the MOST RECENT user message (see above)."
            "\n- Do NOT extract tasks from previous messages or make up tasks that don't exist in the most recent user message."
            "\n- If the user requested multiple tasks in one message, extract ONE task per call."
            "\n- Extract tasks in the ORDER they appear in the MOST RECENT user message, but SKIP any that have already been created."
            "\n- Extract the task description and time exactly as the user specified for THAT specific task."
            "\n- If all requested tasks from the most recent user message have been created, return an error with 'status': 'all_tasks_created'."
            "\n\nTIME INTERPRETATION RULES (CRITICAL):"
            "\n- ALWAYS resolve relative phrases using the user context above."
            "\n- If the user provides a time with NO relative phrase and NO explicit date, schedule it for the CURRENT DATE in the user's timezone."
            "\n- For 'today', 'tonight', 'this evening', 'this afternoon', 'this morning', 'this noon': ALWAYS use the CURRENT calendar date in the user's timezone, regardless of what time it is now."
            "\n  * Example: If current date is November 29, 2025 and user says 'tonight at 9:30', use November 29, 2025 at 9:30 PM - NOT November 30."
            "\n  * 'Tonight' means the night of the CURRENT date, not tomorrow night."
            "\n- For 'tomorrow': use the next calendar date in the user's timezone."
            "\n  * Example: If current date is January 24, 2026 and user says 'tomorrow night at 9:30', use January 25, 2026 at 9:30 PM."
            "\n  * Example: If current date is January 24, 2026 and user says 'tomorrow morning at 9:30', use January 25, 2026 at 9:30 AM"
            "\n- NEVER roll an ambiguous time (like 'at 11pm') to the next day. Keep it on the current date and let the server validate it."
            "\n- Return the datetime in full ISO 8601 with timezone offset (e.g., 2026-01-20T23:00:00-08:00 for 11pm on January 20)."
        )

        print(f"System content: {system_content}")
        
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
                        "task_info": {
                            "type": "string",
                            "description": "The information / description of the task to create.",
                        },
                        "time_to_execute": {
                            "type": "string",
                            "description": f"The time when the task should be executed. This MUST be in ISO format with timezone (e.g., '2026-01-17T16:00:00-08:00' for 4pm PST).",
                        },
                    },
                    "required": ["task_info", "time_to_execute"],
                },
            }
        }
        response = call_openai(messages, [selecting_tool])
        arguments = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        task_info = arguments["task_info"]
        time_to_execute_str = arguments["time_to_execute"]
        print(f"Task description: {task_info}")
        print(f"Time to execute: {time_to_execute_str}")

        # Validate: Check if this task has already been created
        task_info_lower = task_info.lower().strip()
        if task_info_lower in created_tasks:
            print(f"⚠️ Task '{task_info}' has already been created. All tasks from the most recent user message have been created.")
            return json.dumps({
                "success": False,
                "message": f"All tasks from the most recent user message have already been created. The task '{task_info}' was already created.",
                "task_id": None,
                "task_info": {"info": task_info},
                "status": "all_tasks_created",
                "time_to_execute": time_to_execute_str,
            })

        # Parse the string date into datetime object
        time_to_execute = datetime.fromisoformat(time_to_execute_str.replace('Z', '+00:00'))
        
        # Ensure the datetime is in the user's timezone (not UTC)
        # The database should store times in the user's timezone, not UTC
        user_timezone = "UTC"
        if user_config:
            user_timezone = user_config.get("timezone", "UTC")
        try:
            user_tz = ZoneInfo(user_timezone)
            # If the datetime is timezone-naive, assume it's in user's timezone and attach it
            if time_to_execute.tzinfo is None:
                time_to_execute = time_to_execute.replace(tzinfo=user_tz)
            # If it's in UTC (offset 0), convert to user's timezone
            elif time_to_execute.utcoffset() and time_to_execute.utcoffset().total_seconds() == 0:
                # This is UTC, convert to user's timezone
                time_to_execute = time_to_execute.astimezone(user_tz)
            # If it already has a non-UTC timezone (like PST with -08:00), keep it as-is
            # This means the AI correctly generated the time in the user's timezone
        except Exception as e:
            print(f"Warning: Failed to set user timezone {user_timezone}: {e}")
            # Continue with the datetime as-is

        # Validate: do NOT allow scheduling in the past. Return error if invalid.
        try:
            now_user = datetime.now(time_to_execute.tzinfo) if time_to_execute.tzinfo else datetime.now(ZoneInfo(user_timezone))
            if time_to_execute <= now_user:
                msg = f"Invalid time: {time_to_execute.isoformat()} is in the past relative to now ({now_user.isoformat()}) in timezone {user_timezone}. Please ask the user for a new time."
                print(f"⚠️ {msg}")
                raise ValueError(msg)
        except Exception as e:
            # If validation fails (past time or error), do NOT create the task
            error_msg = str(e) if isinstance(e, ValueError) else f"Failed to validate time: {time_to_execute.isoformat()}. Error: {str(e)}. Please ask the user for a valid future time."
            print(f"⚠️ {error_msg}")
            return json.dumps({
                "success": False,
                "message": error_msg,
                "task_id": None,
                "task_info": {"info": task_info},
                "status": "invalid_time",
                "time_to_execute": time_to_execute.isoformat(),
            })

        # Hardcoded UID for now
        user_id = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"

        # Generate UUID for task_id
        task_id = str(uuid.uuid4())

        # Create taskInfo JSON object
        task_info = {"info": task_info}

        # Set initial status
        status = "pending"

        # Execute PostgreSQL query to insert task
        try:
            query = """
                INSERT INTO tasks (task_id, user_id, "task_info", status, time_to_execute)
                VALUES (%s, %s, %s, %s, %s)
            """
            rows_affected = execute_update(query, (task_id, user_id, Json(task_info), status, time_to_execute))
            print(f"Task created. Rows affected: {rows_affected}")
            
            # Enqueue task to Service Bus
            enqueue_result = None
            try:
                enqueue_result = enqueue_task(
                    task_id=task_id,
                    user_id=user_id,
                    task_info=task_info,
                    time_to_execute=time_to_execute.isoformat()
                )
                print(f"Task enqueued to Service Bus: {enqueue_result}")
            except Exception as e:
                print(f"Warning: Failed to enqueue task to Service Bus: {e}")
                # Continue even if enqueueing fails - task is already in database
            
            response_data = {
                "success": True,
                "message": f"Task '{task_info}' created successfully. We don't need another create task tool call for this user instruction unless the user has asked for more tasks than this one you just created.",
                "task_id": task_id,
                "task_info": task_info,
                "status": status,
                "time_to_execute": time_to_execute.isoformat(),
            }
            
            # Add enqueue result if available
            if enqueue_result:
                response_data["enqueue_result"] = enqueue_result
            
            return json.dumps(response_data)
        except Exception as e:
            print(f"Error creating task in database: {e}")
            return json.dumps({
                "success": False,
                "message": f"Error creating task: {str(e)}",
            })

