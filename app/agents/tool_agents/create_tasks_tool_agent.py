import json
import uuid
from datetime import datetime
from database import execute_update
from psycopg2.extras import Json

from ..openai_client import call_openai
from ...task_enqueue import enqueue_task

class CreateTasksToolAgent:
    name = "create_tasks_tool"
    description = "Create a new task with a description and time to execute. Use this tool at most once for each user instruction unless the user explicitly asks for multiple tasks. You don't want to create more or lesstasks than what the user asks for."

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

