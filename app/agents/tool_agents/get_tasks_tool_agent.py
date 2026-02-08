import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import sys
import os
from database import execute_query

from ..openai_client import call_openai

class GetTasksToolAgent:
    name = "get_tasks_tool"
    description = "Get a list of tasks for a given time range. Use this tool ONLY for read-only queries like 'What tasks do I have', 'Show me my tasks', 'When do I have X', etc. NEVER use this tool to create tasks."

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history, user_config=None):
        # Build system message with explicit date interpretation rules
        system_content = (
            f"Given the chat history {chat_history}, and the user config {user_config}, the assistant has decided to use the {self.name}. "
            "CRITICAL DATE INTERPRETATION RULES:\n"
            "- If the user asks for tasks 'today', use the CURRENT CALENDAR DATE from 00:00:00 to 23:59:59 in the user's timezone, NOT a 24-hour window from now.\n"
            "- If the user asks for tasks 'tomorrow', use the NEXT CALENDAR DATE from 00:00:00 to 23:59:59 in the user's timezone.\n"
            "- If the user asks for tasks 'this week', use the current week (Monday to Sunday) in the user's timezone.\n"
            "- If no specific date is mentioned, default to 'today' (current calendar date).\n"
            "- ALWAYS use calendar dates, not 24-hour windows from the current time.\n"
        )
        
        if user_config:
            current_date_str = user_config.get("current_date_str", "")
            current_time_str = user_config.get("current_time_str", "")
            user_timezone_str = user_config.get("timezone", "UTC")
            system_content += (
                f"\nCurrent context: Today is {current_date_str}, current time is {current_time_str} in timezone {user_timezone_str}. "
                f"When the user asks for 'today', use {current_date_str} from 00:00:00 to 23:59:59 in {user_timezone_str}."
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
                        "start_time": {
                            "type": "string",
                            "description": "The start time of the time range to get tasks for. This should be in the format of a python datetime with timezone. For 'today', use 00:00:00 of the current calendar date. For 'tomorrow', use 00:00:00 of the next calendar date.",
                        },
                        "end_time": {
                            "type": "string",
                            "description": "The end time of the time range to get tasks for. This should be in the format of a python datetime with timezone. For 'today', use 23:59:59 of the current calendar date. For 'tomorrow', use 23:59:59 of the next calendar date.",
                        },
                    },
                    "required": ["start_time", "end_time"],
                },
            }
        }
        response = call_openai(messages, [selecting_tool])
        arguments = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        start_time_str = arguments["start_time"]
        end_time_str = arguments["end_time"]
        print(f"Start time: {start_time_str}")
        print(f"End time: {end_time_str}")

        # Parse the string dates into datetime objects
        start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
        end_time = datetime.fromisoformat(end_time_str.replace('Z', '+00:00'))

        # Get user_id from user_config
        user_id = None
        if user_config and user_config.get("user_info"):
            user_id = user_config["user_info"].get("user_id")
        
        if not user_id:
            # Fallback to hardcoded UID if not available in config
            user_id = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
            print(f"Warning: user_id not found in user_config, using fallback: {user_id}")

        # Ensure times have timezone info - if not, use user's timezone from config
        if not start_time.tzinfo:
            if user_config and user_config.get("timezone"):
                user_timezone = user_config["timezone"]
                # Handle UTC specially - ZoneInfo doesn't support "UTC" as a timezone name
                if user_timezone.upper() == "UTC":
                    user_tz = timezone.utc
                else:
                    try:
                        user_tz = ZoneInfo(user_timezone)
                    except Exception:
                        # Fallback to UTC if timezone is invalid
                        user_tz = timezone.utc
                start_time = start_time.replace(tzinfo=user_tz)
            else:
                start_time = start_time.replace(tzinfo=timezone.utc)
        
        if not end_time.tzinfo:
            if user_config and user_config.get("timezone"):
                user_timezone = user_config["timezone"]
                # Handle UTC specially - ZoneInfo doesn't support "UTC" as a timezone name
                if user_timezone.upper() == "UTC":
                    user_tz = timezone.utc
                else:
                    try:
                        user_tz = ZoneInfo(user_timezone)
                    except Exception:
                        # Fallback to UTC if timezone is invalid
                        user_tz = timezone.utc
                end_time = end_time.replace(tzinfo=user_tz)
            else:
                end_time = end_time.replace(tzinfo=timezone.utc)

        # Convert query times to UTC for comparison
        # psycopg2 returns times as UTC (as seen in terminal output), so we normalize query times to UTC
        # This ensures timezone-aware comparison works correctly regardless of how times are stored
        start_time_utc = start_time.astimezone(timezone.utc) if start_time.tzinfo else start_time.replace(tzinfo=timezone.utc)
        end_time_utc = end_time.astimezone(timezone.utc) if end_time.tzinfo else end_time.replace(tzinfo=timezone.utc)

        # Execute PostgreSQL query with time range filter
        # PostgreSQL will handle timezone-aware comparison automatically
        try:
            query = """
                SELECT * FROM tasks 
                WHERE user_id = %s
                AND time_to_execute >= %s
                AND time_to_execute <= %s
            """
            tasks = execute_query(query, (user_id, start_time_utc, end_time_utc))
            print(f"Tasks: {tasks}")
            
            # Convert datetime objects to ISO format strings for JSON serialization
            # Also convert UTC times to user's timezone if user_config is provided
            serializable_tasks = []
            user_tz = None
            if user_config:
                user_timezone = user_config.get("timezone", "UTC")
                try:
                    # Handle UTC specially - ZoneInfo doesn't support "UTC" as a timezone name
                    if user_timezone.upper() == "UTC":
                        user_tz = timezone.utc
                    else:
                        user_tz = ZoneInfo(user_timezone)
                except Exception as e:
                    print(f"Warning: Failed to get user timezone {user_timezone}: {e}")
            
            for task in tasks:
                serializable_task = dict(task)
                # Convert datetime objects to ISO format strings
                if 'time_to_execute' in serializable_task and serializable_task['time_to_execute']:
                    time_to_execute = serializable_task['time_to_execute']
                    # If the time is in UTC and we have user's timezone, convert it
                    if user_tz and time_to_execute.tzinfo:
                        # Check if it's UTC (offset is 0 or None for UTC)
                        offset = time_to_execute.utcoffset()
                        is_utc = (offset is not None and offset.total_seconds() == 0) or str(time_to_execute.tzinfo) == "UTC"
                        if is_utc:
                            # Convert from UTC to user's timezone
                            time_to_execute = time_to_execute.astimezone(user_tz)
                        # If it's timezone-naive, assume it's in user's timezone
                    elif user_tz and time_to_execute.tzinfo is None:
                        time_to_execute = time_to_execute.replace(tzinfo=user_tz)
                    serializable_task['time_to_execute'] = time_to_execute.isoformat()
                serializable_tasks.append(serializable_task)
            
            return json.dumps({
                "tasks": serializable_tasks,
                "total_count": len(serializable_tasks),
            })
        except Exception as e:
            print(f"Error fetching tasks from database: {e}")