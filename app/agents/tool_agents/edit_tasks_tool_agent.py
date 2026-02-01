import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from database import execute_query, execute_update
from psycopg2.extras import Json

from ..openai_client import call_openai

class EditTasksToolAgent:
    name = "edit_tasks_tool"
    description = "Edit an existing task's status, task_info, or time_to_execute. Use this tool when: (1) the user CLEARLY indicates they have completed a task (e.g., 'I completed X', 'I finished Y', 'I did Z', 'I took my medicine', 'just did it') to mark it as completed, OR (2) the user wants to defer the task (e.g., 'I'll do it later', 'not yet', 'I need more time', 'I haven't finished', 'I'm not done yet', 'remind me later') to defer it by 5 minutes. IMPORTANT: Do NOT use this tool if the user ONLY says 'thanks' or 'okay' without clear completion or deferral indication - in those cases, ask for clarification instead. This tool should only be called once the agent has a specific task_id from chat history / previous tool calls (from get_tasks_tool or create_tasks_tool results). NEVER use this tool to create new tasks or to read task information. NEVER use this tool if the task_id is not available in the chat history."

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
        available_tasks = []
        for msg in chat_history:
            # Check for get_tasks_tool results
            if msg.get("name") == "get_tasks_tool" and msg.get("content"):
                try:
                    content = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                    if content.get("tasks"):
                        for task in content.get("tasks", []):
                            if task.get("task_id"):
                                available_tasks.append({
                                    "task_id": task.get("task_id"),
                                    "task_info": task.get("task_info", {}),
                                    "status": task.get("status", "pending"),
                                    "time_to_execute": task.get("time_to_execute")
                                })
                except:
                    pass
            # Check for create_tasks_tool results
            elif msg.get("name") == "create_tasks_tool" and msg.get("content"):
                try:
                    content = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                    if content.get("success") and content.get("task_id"):
                        available_tasks.append({
                            "task_id": content.get("task_id"),
                            "task_info": content.get("task_info", {}),
                            "status": content.get("status", "pending"),
                            "time_to_execute": content.get("time_to_execute")
                        })
                except:
                    pass
            # Check for edit_tasks_tool results (these contain the most up-to-date task state)
            elif msg.get("name") == "edit_tasks_tool" and msg.get("content"):
                try:
                    content = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                    if content.get("success") and content.get("task_id"):
                        available_tasks.append({
                            "task_id": content.get("task_id"),
                            "task_info": content.get("task_info", {}),
                            "status": content.get("status", "pending"),
                            "time_to_execute": content.get("time_to_execute")
                        })
                except:
                    pass
            # Check any message (user or assistant) for embedded JSON task data (e.g., from task reminders)
            # The chat history from general_thinking_agent includes all user input, so we can extract task info from any message
            elif msg.get("content"):
                try:
                    content = msg["content"]
                    # Try to find JSON task data in the message content
                    # The JSON may be appended to text, so try to find it at the end
                    import re
                    
                    # Strategy 1: Find the last occurrence of { that might contain task_id
                    brace_start = content.rfind('{')
                    if brace_start != -1:
                        # Try to extract JSON from this position to the end
                        remaining = content[brace_start:]
                        # Try to parse as JSON
                        try:
                            task_data = json.loads(remaining)
                            if task_data.get("task_id"):
                                available_tasks.append({
                                    "task_id": task_data.get("task_id"),
                                    "task_info": task_data.get("task_info", {}),
                                    "status": task_data.get("status", "pending"),
                                    "time_to_execute": task_data.get("time_to_execute")
                                })
                                continue  # Successfully extracted, move to next message
                        except json.JSONDecodeError:
                            pass
                    
                    # Strategy 2: If parsing from last { failed, try to find JSON object with proper brace matching
                    # Look for "task_id" and then find the enclosing JSON object
                    task_id_pos = content.find('"task_id"')
                    if task_id_pos != -1:
                        # Find the opening brace before "task_id"
                        brace_start = content.rfind('{', 0, task_id_pos)
                        if brace_start != -1:
                            # Find the matching closing brace by counting braces
                            brace_count = 0
                            brace_end = -1
                            for i in range(brace_start, len(content)):
                                if content[i] == '{':
                                    brace_count += 1
                                elif content[i] == '}':
                                    brace_count -= 1
                                    if brace_count == 0:
                                        brace_end = i + 1
                                        break
                            
                            if brace_end > brace_start:
                                try:
                                    json_str = content[brace_start:brace_end]
                                    task_data = json.loads(json_str)
                                    if task_data.get("task_id"):
                                        available_tasks.append({
                                            "task_id": task_data.get("task_id"),
                                            "task_info": task_data.get("task_info", {}),
                                            "status": task_data.get("status", "pending"),
                                            "time_to_execute": task_data.get("time_to_execute")
                                        })
                                except (json.JSONDecodeError, ValueError):
                                    pass
                except Exception as e:
                    # Silently continue if extraction fails
                    pass

        # Deduplicate available_tasks by task_id, keeping only the most recent entry
        # (later entries in chat_history are more recent)
        task_id_to_task = {}
        for task in available_tasks:
            task_id = task.get("task_id")
            if task_id:
                # If we've seen this task_id before, replace it (keep the most recent)
                task_id_to_task[task_id] = task
        
        # Convert back to list
        available_tasks = list(task_id_to_task.values())
        
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
                f"\n- You MUST identify which task the user wants to edit by finding the specific task_id from the chat history."
                f"\n- You MUST determine what fields the user wants to edit: status, task_info (description), or time_to_execute."
                f"\n- You MUST extract the exact task_id from previous tool call results - do NOT make up or guess a task_id."
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
                f"\n\n- You MUST select the correct task_id from the list above based on the user's request."
                f"\n- If the user refers to a task by description, match it to the task_id from the list above."
            )
        else:
            system_content += (
                f"\n\n⚠️ ERROR: No tasks with task_id found in chat history from previous tool calls."
                f"\n- This tool REQUIRES a specific task_id from previous get_tasks_tool or create_tasks_tool results."
                f"\n- You CANNOT proceed without a task_id. Return an error indicating that task_id is required."
            )

        system_content += (
            "\n\nTASK EDITING RULES:"
            "\n- CRITICAL: You MUST have a specific task_id from chat history to proceed. If no task_id is available, you MUST return an error."
            "\n- Extract the exact task_id from the chat history (from previous get_tasks_tool or create_tasks_tool results)."
            "\n- You can update one or more of the following fields: status, task_info, or time_to_execute."
            "\n- For STATUS updates:"
            "\n  * CRITICAL: This tool should be called when the user CLEARLY indicates they have completed a task (e.g., 'I completed X', 'I finished Y', 'I did Z', 'I'm done with X', 'I took my medicine', 'just did it') → use status 'completed'"
            "\n  * If the user says 'mark as complete', 'complete', 'done', 'finished' → use status 'completed'"
            "\n  * If the user says 'mark as pending', 'uncomplete', 'reopen', 'undo' → use status 'pending'"
            "\n  * Valid statuses are: 'pending' or 'completed'"
            "\n  * CRITICAL: When marking a task as completed, ONLY update the status field. Do NOT change task_info or time_to_execute."
            "\n- For TASK_INFO updates:"
            "\n  * Extract the new task description/info from the user's request"
            "\n  * Format it as a string (the task_info will be stored as JSON with 'info' field)"
            "\n- For TIME_TO_EXECUTE updates:"
            "\n  * CRITICAL: If the user wants to defer the task (e.g., 'I'll do it later', 'not yet', 'I need more time', 'I haven't finished X', 'I'm not done with Y', 'I'm not done yet', 'remind me later', 'not finished', 'I can't do that right now') → defer the task by 5 minutes"
            "\n  * To defer by 5 minutes:"
            "\n    - Look at the task's current time_to_execute from the AVAILABLE TASKS list above"
            "\n    - IMPORTANT: The time shown is ALREADY in the user's timezone ({timezone})"
            "\n    - IMPORTANT: If there are multiple entries for the same task_id, use the MOST RECENT time_to_execute (the one that appears later in the list)"
            "\n    - Parse that time_to_execute datetime (it's already in {timezone} timezone)"
            "\n    - Add EXACTLY 5 minutes to it (do NOT change the timezone)"
            "\n    - If the task's current time_to_execute is in the past (before current time), use current time + 5 minutes instead"
            "\n    - Current time context: {current_time_str} on {current_date_str} in timezone {timezone}"
            "\n    - Return the new datetime in full ISO 8601 with timezone offset matching the user's timezone (e.g., for PST: 2026-01-20T23:05:00-08:00)"
            "\n    - CRITICAL: The time shown in AVAILABLE TASKS is already converted to {timezone} - do NOT add 8 hours or convert timezones again, just add 5 minutes"
            "\n  * For other time updates: Extract the new time from the user's request"
            "\n  * Use the current date context: {current_date_str}"
            "\n  * If the user says 'tonight', 'today', 'this evening', etc., they mean {current_date_str} - NOT tomorrow"
            "\n  * ONLY use tomorrow's date if the user explicitly says 'tomorrow'"
            "\n- Only include fields in the update that the user explicitly wants to change. Do NOT include fields that the user did not mention."
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
                            "description": "The specific task_id of the task to edit. This MUST be extracted from previous tool call results in the chat history (from get_tasks_tool or create_tasks_tool). If no task_id is available in chat history, you cannot proceed and must indicate this is an error.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "completed"],
                            "description": "The new status for the task. Use 'completed' if the user wants to mark the task as done/complete/finished. Use 'pending' if the user wants to mark the task as pending/uncomplete/reopen. CRITICAL: When marking as completed, ONLY include this field - do NOT include task_info or time_to_execute. Only include this field if the user wants to change the status.",
                        },
                        "task_info": {
                            "type": "string",
                            "description": "The new task description/information. Only include this field if the user wants to change the task description.",
                        },
                        "time_to_execute": {
                            "type": "string",
                            "description": f"The new time when the task should be executed. This MUST be in ISO format with timezone (e.g., '2026-01-17T16:00:00-08:00' for 4pm PST). Only include this field if the user wants to change the execution time.",
                        },
                    },
                    "required": ["task_id"],
                },
            }
        }
        response = call_openai(messages, [selecting_tool])
        arguments = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        task_id = arguments["task_id"]
        new_status = arguments.get("status")
        new_task_info = arguments.get("task_info")
        new_time_to_execute_str = arguments.get("time_to_execute")
        print(f"Task ID to edit: {task_id}")
        print(f"New status: {new_status}")
        print(f"New task_info: {new_task_info}")
        print(f"New time_to_execute: {new_time_to_execute_str}")

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
        
        # Validate that at least one field is being updated
        if not new_status and not new_task_info and not new_time_to_execute_str:
            return json.dumps({
                "success": False,
                "message": "At least one field (status, task_info, or time_to_execute) must be provided to update the task.",
                "task_id": task_id,
            })
        
        # Validate status if provided
        if new_status and new_status not in ["pending", "completed"]:
            return json.dumps({
                "success": False,
                "message": f"Invalid status: {new_status}. Status must be 'pending' or 'completed'.",
                "task_id": task_id,
            })
        
        # CRITICAL: When marking as completed, only status should be updated
        if new_status == "completed" and (new_task_info or new_time_to_execute_str):
            return json.dumps({
                "success": False,
                "message": "When marking a task as completed, only the status field should be updated. Do not change task_info or time_to_execute.",
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

        # Verify task exists and belongs to user, then update status
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
            
            # Build dynamic UPDATE query based on what fields need to be updated
            updates = []
            params = []
            
            # Handle status update
            if new_status:
                updates.append("status = %s")
                params.append(new_status)
            
            # Handle task_info update
            if new_task_info:
                # Format task_info as JSON object with 'info' field
                task_info_dict = {"info": new_task_info}
                updates.append('"task_info" = %s')
                params.append(Json(task_info_dict))
            
            # Handle time_to_execute update
            time_to_execute_dt = None
            if new_time_to_execute_str:
                # Parse the time string
                time_to_execute_dt = datetime.fromisoformat(new_time_to_execute_str.replace('Z', '+00:00'))
                
                # Preserve the timezone as provided - DB doesn't enforce UTC
                # If timezone-naive, assume it's in user's timezone
                if time_to_execute_dt.tzinfo is None:
                    user_timezone = "UTC"
                    if user_config:
                        user_timezone = user_config.get("timezone", "UTC")
                    try:
                        user_tz = ZoneInfo(user_timezone) if user_timezone.upper() != "UTC" else timezone.utc
                        time_to_execute_dt = time_to_execute_dt.replace(tzinfo=user_tz)
                    except Exception as e:
                        print(f"Warning: Failed to set user timezone {user_timezone}: {e}")
                        # If timezone setting fails, assume UTC
                        time_to_execute_dt = time_to_execute_dt.replace(tzinfo=timezone.utc)
                # If it has timezone info, keep it as-is (preserve whatever timezone was provided)
                
                updates.append("time_to_execute = %s")
                params.append(time_to_execute_dt)
            
            # If no updates, return error
            if not updates:
                return json.dumps({
                    "success": False,
                    "message": "No fields to update. At least one field (status, task_info, or time_to_execute) must be provided.",
                    "task_id": task_id,
                })
            
            # Add task_id to params for WHERE clause
            params.append(task_id)
            
            # Build and execute UPDATE query
            update_query = f"""
                UPDATE tasks
                SET {', '.join(updates)}
                WHERE task_id = %s
            """
            rows_affected = execute_update(update_query, tuple(params))
            
            # Build success message
            update_messages = []
            if new_status:
                update_messages.append(f"status to '{new_status}'")
            if new_task_info:
                update_messages.append("task_info")
            if new_time_to_execute_str:
                update_messages.append("time_to_execute")
            success_message = f"Task updated successfully ({', '.join(update_messages)})."
            
            print(f"Task updated. Task ID: {task_id}, Rows affected: {rows_affected}")
            
            # Fetch updated task to return in response
            updated_tasks = execute_query(query, (task_id,))
            if not updated_tasks or len(updated_tasks) == 0:
                # This shouldn't happen, but handle it gracefully
                return json.dumps({
                    "success": False,
                    "message": f"Task was updated but could not be retrieved.",
                    "task_id": task_id,
                })
            
            updated_task = dict(updated_tasks[0])
            
            # Convert datetime to ISO format if present
            task_info = updated_task.get("task_info")
            time_to_execute = updated_task.get("time_to_execute")
            if time_to_execute:
                time_to_execute = time_to_execute.isoformat() if hasattr(time_to_execute, 'isoformat') else str(time_to_execute)
            
            return json.dumps({
                "success": True,
                "message": success_message,
                "task_id": task_id,
                "task_info": task_info,
                "status": updated_task.get("status"),
                "time_to_execute": time_to_execute,
            })
        except Exception as e:
            print(f"Error updating task in database: {e}")
            return json.dumps({
                "success": False,
                "message": f"Error updating task: {str(e)}",
                "task_id": task_id,
            })
