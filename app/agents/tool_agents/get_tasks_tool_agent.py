import json
from datetime import datetime, timezone

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

        # REPLACE WITH POSTGRES QUERY
        return json.dumps({
            "tasks": mock_tasks,
            "total_count": len(mock_tasks),
            "time_range": {
                "start": start_time.isoformat() if start_time else None,
                "end": end_time.isoformat() if end_time else None
            }
        })

mock_tasks = [
        {
            "id": 1,
            "title": "Morning workout",
            "description": "30-minute cardio session at the gym",
            "status": "pending",
            "scheduled_time": "2024-01-15T07:00:00Z",
            "category": "health"
        },
        {
            "id": 2,
            "title": "Team standup meeting",
            "description": "Daily standup with development team",
            "status": "pending",
            "scheduled_time": "2024-01-15T09:00:00Z",
            "category": "work"
        },
        {
            "id": 3,
            "title": "Review project proposal",
            "description": "Review and provide feedback on Q1 project proposal",
            "status": "pending",
            "scheduled_time": "2024-01-15T10:00:00Z",
            "category": "work"
        },
        {
            "id": 4,
            "title": "Lunch with client",
            "description": "Business lunch meeting with potential client",
            "status": "pending",
            "scheduled_time": "2024-01-15T12:00:00Z",
            "category": "work"
        },
        {
            "id": 5,
            "title": "Grocery shopping",
            "description": "Buy ingredients for weekend meal prep",
            "status": "pending",
            "scheduled_time": "2024-01-15T18:00:00Z",
            "category": "personal"
        },
        {
            "id": 6,
            "title": "Call dentist",
            "description": "Schedule annual checkup",
            "status": "pending",
            "scheduled_time": "2024-01-15T19:00:00Z",
            "category": "personal"
        },
        {
            "id": 7,
            "title": "Update project documentation",
            "description": "Update API documentation for new features",
            "status": "in_progress",
            "scheduled_time": "2024-01-15T14:00:00Z",
            "category": "work"
        },
        {
            "id": 8,
            "title": "Evening meditation",
            "description": "20-minute mindfulness meditation session",
            "status": "pending",
            "scheduled_time": "2024-01-15T20:00:00Z",
            "category": "health"
        }
    ]