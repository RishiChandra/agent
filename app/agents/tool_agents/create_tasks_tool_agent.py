import json
import uuid
from datetime import datetime
from database import execute_update
from psycopg2.extras import Json

from ..openai_client import call_openai

class CreateTasksToolAgent:
    name = "create_tasks_tool"
    description = "Create a new task with a description and time to execute"

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
                        "task_info": {
                            "type": "string",
                            "description": "The information / description of the task to create.",
                        },
                        "time_to_execute": {
                            "type": "string",
                            "description": "The time when the task should be executed. This should be in the format of a python datetime with timezone.",
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

        # Parse the string date into datetime object
        time_to_execute = datetime.fromisoformat(time_to_execute_str.replace('Z', '+00:00'))

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
            
            return json.dumps({
                "success": True,
                "message": f"Task '{task_info}' created successfully",
                "task_id": task_id,
                "task_info": task_info,
                "status": status,
                "time_to_execute": time_to_execute.isoformat(),
            })
        except Exception as e:
            print(f"Error creating task in database: {e}")
            return json.dumps({
                "success": False,
                "message": f"Error creating task: {str(e)}",
            })

