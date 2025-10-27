import json
from datetime import datetime, timezone
import sys
import os
from database import execute_query

from ..openai_client import call_openai

class GetTasksToolAgent:
    name = "get_tasks_tool"
    description = "Get a list of tasks for a given time range"

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history):
        messages = [
            {"role": "system", "content": f"Given the chat history {chat_history}, the assistant has decided to use the {self.name}"},
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
                            "description": "The start time of the time range to get tasks for. This should be in the format of a python datetime with timezone.",
                        },
                        "end_time": {
                            "type": "string",
                            "description": "The start time of the time range to get tasks for. This should be in the format of a python datetime with timezone.",
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

        # Hardcoded UID for now
        uid = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"

        # Execute PostgreSQL query
        try:
            query = """
                SELECT * FROM tasks 
                WHERE user_id = %s
            """
            tasks = execute_query(query, (uid,))
            print(f"Tasks: {tasks}")
            
            # Convert datetime objects to ISO format strings for JSON serialization
            serializable_tasks = []
            for task in tasks:
                serializable_task = dict(task)
                # Convert datetime objects to ISO format strings
                if 'time_to_execute' in serializable_task and serializable_task['time_to_execute']:
                    serializable_task['time_to_execute'] = serializable_task['time_to_execute'].isoformat()
                serializable_tasks.append(serializable_task)
            
            return json.dumps({
                "tasks": serializable_tasks,
                "total_count": len(serializable_tasks),
            })
        except Exception as e:
            print(f"Error fetching tasks from database: {e}")