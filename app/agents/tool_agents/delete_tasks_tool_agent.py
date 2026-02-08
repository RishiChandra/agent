import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from database import execute_query, execute_update

from ..openai_client import call_openai
from ..utils.task_extraction_utils import extract_tasks_from_chat_history

class DeleteTasksToolAgent:
    name = "delete_tasks_tool"
    description = "Delete an existing task. Use this tool when the user explicitly asks to DELETE, REMOVE, or CANCEL a task. IMPORTANT: This tool should only be called once the agent has a specific task_id from chat history / previous tool calls (from get_tasks_tool or create_tasks_tool results). NEVER use this tool to create new tasks or to read task information. NEVER use this tool if the task_id is not available in the chat history."

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history, user_config=None):
        # Build system message with context
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

        # Extract task information from chat history
        # Look for task_id from previous get_tasks_tool or create_tasks_tool results
        # Also check all messages (including user messages) for embedded JSON task data (e.g., from task reminders)
        available_tasks = extract_tasks_from_chat_history(chat_history)
        
        # If we have task_ids, fetch current state from database to ensure accuracy
        if available_tasks and user_config and user_config.get("user_info"):
            user_id = user_config["user_info"].get("user_id")
            if user_id:
                for task in available_tasks:
                    task_id = task.get("task_id")
                    if task_id:
                        try:
                            # Fetch current state from database
                            query = """
                                SELECT task_id, user_id, task_info, status, time_to_execute
                                FROM tasks
                                WHERE task_id = %s AND user_id = %s
                            """
                            db_tasks = execute_query(query, (task_id, user_id))
                            if db_tasks and len(db_tasks) > 0:
                                db_task = dict(db_tasks[0])
                                # Update with current database state
                                task["task_info"] = db_task.get("task_info", {})
                                task["status"] = db_task.get("status", "pending")
                                task["time_to_execute"] = db_task.get("time_to_execute")
                        except Exception as e:
                            # If database fetch fails, continue with chat history data
                            print(f"Warning: Could not fetch current task state from database: {e}")

        system_content = (
            f"Given the chat history {chat_history}, the assistant has decided to use the {self.name}."
            f"\n\nUSER CONTEXT:\n- User name: {user_name}\n- Current time: {current_time_str}\n- Current date: {current_date_str}\n- User timezone: {timezone}"
        )

        if most_recent_user_message:
            system_content += (
                f"\n\n⚠️ CRITICAL: The MOST RECENT user message is: \"{most_recent_user_message}\""
                f"\n- You MUST identify which task the user wants to delete by finding the specific task_id from the chat history."
                f"\n- You MUST extract the exact task_id from previous tool call results - do NOT make up or guess a task_id."
                f"\n- CRITICAL MATCHING REQUIREMENT: You MUST match BOTH the task description AND the time mentioned by the user."
                f"\n  * Extract the task description (what the user wants to delete) from the most recent user message."
                f"\n  * Extract the time mentioned by the user (if any) from the most recent user message."
                f"\n  * Find the task_id that matches BOTH the description AND the time from the AVAILABLE TASKS list below."
                f"\n  * If the user mentions a time (e.g., 'at 6am', 'at 9pm', 'tomorrow at 3pm'), you MUST match that specific time."
                f"\n  * If the user only mentions a description without a time, match by description but verify it's the correct task."
            )

        if available_tasks:
            system_content += (
                f"\n\n⚠️ AVAILABLE TASKS FROM CHAT HISTORY:"
            )
            for i, task in enumerate(available_tasks, 1):
                task_info_str = task.get("task_info", {})
                if isinstance(task_info_str, dict):
                    task_info_str = task_info_str.get("info", str(task_info_str))
                else:
                    task_info_str = str(task_info_str)
                
                # Convert time_to_execute to user's timezone for display
                # DB doesn't enforce UTC - convert whatever timezone it's stored in to user's timezone
                time_to_execute_display = task.get('time_to_execute', 'N/A')
                if time_to_execute_display != 'N/A' and isinstance(time_to_execute_display, datetime):
                    try:
                        user_tz = ZoneInfo(timezone)
                        # Convert to user's timezone (no UTC assumptions)
                        if time_to_execute_display.tzinfo:
                            # Has timezone info - convert to user's timezone
                            time_to_execute_display = time_to_execute_display.astimezone(user_tz)
                        else:
                            # Timezone-naive - assume it's already in user's timezone and attach timezone info
                            time_to_execute_display = time_to_execute_display.replace(tzinfo=user_tz)
                        # Format in user's timezone with offset
                        time_to_execute_display = time_to_execute_display.strftime("%Y-%m-%d %H:%M:%S %Z (%z)")
                    except Exception as e:
                        print(f"Warning: Could not convert time_to_execute to user timezone: {e}")
                        time_to_execute_display = str(time_to_execute_display)
                elif time_to_execute_display != 'N/A':
                    time_to_execute_display = str(time_to_execute_display)
                
                system_content += (
                    f"\n{i}. Task ID: {task.get('task_id')}"
                    f"\n   Description: {task_info_str}"
                    f"\n   Current Status: {task.get('status', 'pending')}"
                    f"\n   Time to Execute: {time_to_execute_display} (in user's timezone: {timezone})"
                )
            system_content += (
                f"\n\n⚠️ TASK MATCHING REQUIREMENTS:"
                f"\n- You MUST select the task_id that matches BOTH the description AND time from the user's most recent message."
                f"\n- Compare the task description mentioned by the user with the 'Description' field in the list above."
                f"\n- Compare the time mentioned by the user (if any) with the 'Time to Execute' field in the list above."
                f"\n- The task_id you select MUST match BOTH the description AND time - do NOT select a task that only matches one."
                f"\n- If multiple tasks have similar descriptions but different times, you MUST select the one that matches the time the user specified."
                f"\n- If no task matches both the description AND time, return an error - do NOT guess or select a partial match."
            )
        else:
            system_content += (
                f"\n\n⚠️ ERROR: No tasks with task_id found in chat history from previous tool calls."
                f"\n- This tool REQUIRES a specific task_id from previous get_tasks_tool or create_tasks_tool results."
                f"\n- You CANNOT proceed without a task_id. Return an error indicating that task_id is required."
            )

        system_content += (
            "\n\nTASK DELETION RULES:"
            "\n- CRITICAL: You MUST have a specific task_id from chat history to proceed. If no task_id is available, you MUST return an error."
            "\n- Extract the exact task_id from the chat history (from previous get_tasks_tool or create_tasks_tool results)."
            "\n- CRITICAL MATCHING: The task_id you select MUST match BOTH:"
            "\n  1. The task description mentioned by the user in the most recent message"
            "\n  2. The time mentioned by the user in the most recent message (if a time was mentioned)"
            "\n- If the user said 'delete the task to brush my teeth at 6am', you MUST find the task with description matching 'brush my teeth' AND time matching '6am'."
            "\n- If the user said 'delete the task to eat breakfast' (no time), match by description but be careful if there are multiple tasks with similar descriptions."
            "\n- If no task matches both description AND time, return an error - do NOT proceed with a partial match."
            "\n- If the task_id cannot be determined from chat history, you MUST return an error - do NOT proceed."
        )

        # Early validation: if no tasks are available in chat history, return error immediately
        if not available_tasks:
            print(f"⚠️ Error: No tasks with task_id found in chat history.")
            return json.dumps({
                "success": False,
                "message": "No task_id available in chat history. This tool requires a specific task_id from previous get_tasks_tool or create_tasks_tool results. Please first retrieve tasks using get_tasks_tool.",
                "task_id": None,
            })

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
                        "task_id": {
                            "type": "string",
                            "description": "The specific task_id of the task to delete. This MUST be extracted from previous tool call results in the chat history (from get_tasks_tool or create_tasks_tool). CRITICAL: The task_id MUST match BOTH the task description AND time mentioned by the user in the most recent message. If no task_id matches both description and time, or if no task_id is available in chat history, you cannot proceed and must indicate this is an error.",
                        },
                    },
                    "required": ["task_id"],
                },
            }
        }
        response = call_openai(messages, [selecting_tool])
        arguments = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        task_id = arguments["task_id"]
        print(f"Task ID to delete: {task_id}")

        # Validate task_id exists in available tasks from chat history
        # (We already checked that available_tasks is not empty above)
        task_ids = [task.get("task_id") for task in available_tasks]
        if task_id not in task_ids:
            print(f"⚠️ Error: Task ID {task_id} not found in available tasks from chat history.")
            return json.dumps({
                "success": False,
                "message": f"Task ID {task_id} was not found in the chat history from previous tool calls. The task_id must come from previous get_tasks_tool or create_tasks_tool results.",
                "task_id": task_id,
            })
        
        # Get user_id from user_config
        user_id = None
        if user_config and user_config.get("user_info"):
            user_id = user_config["user_info"].get("user_id")
        
        if not user_id:
            # Fallback to hardcoded UID if not available in config
            user_id = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
            print(f"Warning: user_id not found in user_config, using fallback: {user_id}")

        # Verify task exists and belongs to user, then delete it
        try:
            # First, verify task exists and belongs to user
            query = """
                SELECT task_id, user_id, task_info, status, time_to_execute
                FROM tasks
                WHERE task_id = %s
            """
            tasks = execute_query(query, (task_id,))
            
            if not tasks or len(tasks) == 0:
                return json.dumps({
                    "success": False,
                    "message": f"Task with ID {task_id} not found.",
                    "task_id": task_id,
                })
            
            task = dict(tasks[0])
            
            if task.get("user_id") != user_id:
                return json.dumps({
                    "success": False,
                    "message": f"Task with ID {task_id} does not belong to the current user.",
                    "task_id": task_id,
                })
            
            # Store task info for response before deletion
            task_info = task.get("task_info")
            task_info_str = task_info.get("info", "") if isinstance(task_info, dict) else str(task_info)
            
            # Delete the task
            delete_query = """
                DELETE FROM tasks
                WHERE task_id = %s AND user_id = %s
            """
            rows_affected = execute_update(delete_query, (task_id, user_id))
            
            if rows_affected == 0:
                return json.dumps({
                    "success": False,
                    "message": f"Failed to delete task with ID {task_id}. Task may have already been deleted or does not belong to the current user.",
                    "task_id": task_id,
                })
            
            print(f"Task deleted. Task ID: {task_id}, Rows affected: {rows_affected}")
            
            return json.dumps({
                "success": True,
                "message": f"Task '{task_info_str}' (ID: {task_id}) deleted successfully.",
                "task_id": task_id,
                "task_info": task_info,
            })
        except Exception as e:
            print(f"Error deleting task from database: {e}")
            return json.dumps({
                "success": False,
                "message": f"Error deleting task: {str(e)}",
                "task_id": task_id,
            })
