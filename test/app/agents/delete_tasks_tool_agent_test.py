import unittest
import sys
import os
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

# Add the app directory to the Python path to enable imports like "from database import ..."
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
test_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, os.path.join(project_root, 'app'))
sys.path.insert(0, project_root)
sys.path.insert(0, test_dir)

from app.agents.tool_agents.delete_tasks_tool_agent import DeleteTasksToolAgent

# Import test helpers - works for both direct execution and unittest
try:
    # Try relative import first (works when run as module)
    from .test_helpers import (
        are_openai_credentials_configured,
        create_mock_task,
        get_default_user_config,
        DEFAULT_USER_ID
    )
except ImportError:
    # Fall back to direct import (works when run as script)
    import importlib.util
    helpers_path = os.path.join(os.path.dirname(__file__), 'test_helpers.py')
    spec = importlib.util.spec_from_file_location("test_helpers", helpers_path)
    test_helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_helpers)
    are_openai_credentials_configured = test_helpers.are_openai_credentials_configured
    create_mock_task = test_helpers.create_mock_task
    get_default_user_config = test_helpers.get_default_user_config
    DEFAULT_USER_ID = test_helpers.DEFAULT_USER_ID


class DeleteTasksToolAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        # Skip tests if OpenAI credentials are not configured (tests use mocks, but check for consistency)
        if not are_openai_credentials_configured():
            self.skipTest("OpenAI credentials not configured (tests use mocks but check for consistency)")
        
        self.agent = DeleteTasksToolAgent()
        # Use UTC for tests to avoid tzdata dependency issues
        self.user_config = get_default_user_config(timezone="UTC")
    
    def tearDown(self):
        """Clean up after each test."""
        pass
    
    def create_chat_history_with_task(self, task_id, task_info, status="pending", time_to_execute=None, user_message="Delete that task"):
        """Helper to create chat history with a task from get_tasks_tool."""
        if time_to_execute is None:
            time_to_execute = datetime.now(timezone.utc) + timedelta(hours=1)
        
        tasks_response = {
            "tasks": [{
                "task_id": task_id,
                "task_info": task_info,
                "status": status,
                "time_to_execute": time_to_execute.isoformat() if isinstance(time_to_execute, datetime) else time_to_execute
            }],
            "total_count": 1
        }
        
        return [
            {"role": "user", "content": "What tasks do I have?"},
            {"role": "assistant", "name": "get_tasks_tool", "content": json.dumps(tasks_response)},
            {"role": "user", "content": user_message}
        ]
    
    def create_chat_history_with_multiple_tasks(self, tasks_data, user_message="Delete the first task"):
        """Helper to create chat history with multiple tasks."""
        tasks_response = {
            "tasks": tasks_data,
            "total_count": len(tasks_data)
        }
        
        return [
            {"role": "user", "content": "What tasks do I have?"},
            {"role": "assistant", "name": "get_tasks_tool", "content": json.dumps(tasks_response)},
            {"role": "user", "content": user_message}
        ]
    
    def create_chat_history_with_reminder(self, task_id, task_info, time_to_execute, user_message="Delete that task"):
        """Helper to create chat history with a task reminder."""
        reminder_message = f'Tell the user that it is time for them to complete this task now{{"task_id": "{task_id}", "user_id": "{DEFAULT_USER_ID}", "task_info": {json.dumps(task_info)}, "time_to_execute": "{time_to_execute}"}}'
        
        return [
            {"role": "user", "content": reminder_message},
            {"role": "assistant", "content": "Hey, just a reminder to complete your task."},
            {"role": "user", "content": user_message}
        ]
    
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.gemini_response_to_openai_like')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.call_gemini')
    def test_delete_task_success(self, mock_call_gemini, mock_gemini_response_to_openai_like, mock_execute_update, mock_execute_query):
        """Test that delete_tasks_tool_agent can successfully delete a task."""
        task_id = "test-task-1"
        task_info = {"info": "Take my medicine"}
        original_time = datetime.now(timezone.utc) + timedelta(hours=1)
        
        # Mock Gemini response for tool selection (extracting task_id)
        mock_call_gemini.return_value = MagicMock()  # Return any mock, will be converted
        mock_gemini_response_to_openai_like.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called 2 times:
        # 1. Fetch current state for available_tasks (line 52)
        # 2. Verify task exists before deletion (line 214)
        original_task = create_mock_task(
            task_id=task_id,
            task_info=task_info,
            status="pending",
            time_to_execute=original_time
        )
        mock_execute_query.side_effect = [[original_task], [original_task]]
        
        # Mock database update (DELETE)
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Create chat history with task
        chat_history = self.create_chat_history_with_task(
            task_id=task_id,
            task_info=task_info,
            status="pending",
            time_to_execute=original_time,
            user_message="Delete that task"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task deletion should succeed")
        self.assertEqual(result_data.get("task_id"), task_id, "Task ID should match")
        self.assertIn("deleted successfully", result_data.get("message", "").lower(), "Message should indicate successful deletion")
        
        # Verify execute_update was called with DELETE query
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        
        # Verify the delete query (normalize whitespace for comparison)
        query_normalized = " ".join(query.split()).upper()
        self.assertIn("DELETE FROM TASKS", query_normalized, "Should delete from tasks table")
        self.assertEqual(params[0], task_id, "First param should be task_id")
        self.assertEqual(params[1], DEFAULT_USER_ID, "Second param should be user_id")
    
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.gemini_response_to_openai_like')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.call_gemini')
    def test_delete_task_fails_without_task_id(self, mock_call_gemini, mock_gemini_response_to_openai_like, mock_execute_query):
        """Test that delete_tasks_tool_agent returns an error when no task_id is available."""
        # Mock Gemini response - no task_id available
        mock_call_gemini.return_value = MagicMock()  # Return any mock, will be converted
        mock_gemini_response_to_openai_like.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": "nonexistent-task"
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - return empty (task not found in available_tasks)
        mock_execute_query.return_value = []
        
        # Create chat history without any tasks
        chat_history = [
            {"role": "user", "content": "Delete a task"}
        ]
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result indicates failure
        self.assertFalse(result_data.get("success", True), "Task deletion should fail without task_id")
        message = result_data.get("message", "").lower()
        self.assertTrue(
            "task_id" in message or "no task" in message or "not found" in message or "not available" in message,
            f"Error message should indicate task_id issue. Got: {message}"
        )
    
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.gemini_response_to_openai_like')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.call_gemini')
    def test_delete_task_fails_when_task_not_found(self, mock_call_gemini, mock_gemini_response_to_openai_like, mock_execute_update, mock_execute_query):
        """Test that delete_tasks_tool_agent returns an error when task doesn't exist in database."""
        task_id = "nonexistent-task"
        
        # Mock Gemini response
        mock_call_gemini.return_value = MagicMock()
        mock_gemini_response_to_openai_like.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - task not found in available_tasks, so it should return early
        # But if task_id is in available_tasks but not in DB, it will query DB and return empty
        mock_execute_query.return_value = []  # Task not found in database
        
        # Create chat history with task in available_tasks but not in DB
        chat_history = self.create_chat_history_with_task(
            task_id=task_id,
            task_info={"info": "Some task"},
            user_message="Delete that task"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result indicates failure
        self.assertFalse(result_data.get("success", True), "Task deletion should fail when task not found")
        message = result_data.get("message", "").lower()
        self.assertTrue(
            "not found" in message or "does not exist" in message,
            f"Error message should indicate task not found. Got: {message}"
        )
    
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.gemini_response_to_openai_like')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.call_gemini')
    def test_delete_task_fails_when_task_belongs_to_different_user(self, mock_call_gemini, mock_gemini_response_to_openai_like, mock_execute_update, mock_execute_query):
        """Test that delete_tasks_tool_agent returns an error when task belongs to a different user."""
        task_id = "other-user-task"
        other_user_id = "different-user-id"
        task_info = {"info": "Some task"}
        
        # Mock Gemini response
        mock_call_gemini.return_value = MagicMock()
        mock_gemini_response_to_openai_like.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - task exists but belongs to different user
        other_user_task = create_mock_task(
            task_id=task_id,
            user_id=other_user_id,  # Different user
            task_info=task_info,
            status="pending"
        )
        mock_execute_query.side_effect = [[other_user_task], [other_user_task]]  # Available tasks, then verify exists
        
        # Create chat history with task
        chat_history = self.create_chat_history_with_task(
            task_id=task_id,
            task_info=task_info,
            user_message="Delete that task"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result indicates failure
        self.assertFalse(result_data.get("success", True), "Task deletion should fail when task belongs to different user")
        message = result_data.get("message", "").lower()
        self.assertTrue(
            "does not belong" in message or "belong to" in message,
            f"Error message should indicate ownership issue. Got: {message}"
        )
    
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.gemini_response_to_openai_like')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.call_gemini')
    def test_delete_task_with_multiple_tasks_selects_correct_one(self, mock_call_gemini, mock_gemini_response_to_openai_like, mock_execute_update, mock_execute_query):
        """Test that when multiple tasks are in chat history, the right one is selected based on user's mention."""
        task_id_1 = "task-medicine"
        task_id_2 = "task-call"
        
        task_info_1 = {"info": "Take my medicine"}
        task_info_2 = {"info": "Call mom"}
        
        original_time = datetime.now(timezone.utc) + timedelta(hours=1)
        
        # Mock Gemini response - should select task_id_1 based on user saying "medicine"
        mock_call_gemini.return_value = MagicMock()
        mock_gemini_response_to_openai_like.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id_1
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called for each task in available_tasks, then verify exists
        original_task_1 = create_mock_task(
            task_id=task_id_1,
            task_info=task_info_1,
            status="pending",
            time_to_execute=original_time
        )
        original_task_2 = create_mock_task(
            task_id=task_id_2,
            task_info=task_info_2,
            status="pending",
            time_to_execute=original_time + timedelta(hours=2)
        )
        # Calls: fetch current state for both tasks, verify task_1 exists
        mock_execute_query.side_effect = [[original_task_1], [original_task_2], [original_task_1]]
        
        # Mock database update
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Create chat history with multiple tasks
        tasks_data = [
            {
                "task_id": task_id_1,
                "task_info": task_info_1,
                "status": "pending",
                "time_to_execute": original_time.isoformat()
            },
            {
                "task_id": task_id_2,
                "task_info": task_info_2,
                "status": "pending",
                "time_to_execute": (original_time + timedelta(hours=2)).isoformat()
            }
        ]
        
        chat_history = self.create_chat_history_with_multiple_tasks(
            tasks_data=tasks_data,
            user_message="Delete the medicine task"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task deletion should succeed")
        self.assertEqual(result_data.get("task_id"), task_id_1, "Should delete task_id_1 based on user's mention of 'medicine'")
        self.assertNotEqual(result_data.get("task_id"), task_id_2, "Should NOT delete task_id_2 (Call mom)")
        
        # Verify execute_update was called with the right task_id
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        params = call_args[0][1]
        
        # First param should be the task_id in DELETE query
        self.assertEqual(params[0], task_id_1, "Should delete task_id_1 based on user's request")
        self.assertNotEqual(params[0], task_id_2, "Should NOT delete task_id_2")
    
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.gemini_response_to_openai_like')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.call_gemini')
    def test_delete_task_matches_description_and_time(self, mock_call_gemini, mock_gemini_response_to_openai_like, mock_execute_update, mock_execute_query):
        """Test that delete_tasks_tool_agent matches both description and time when selecting task to delete."""
        task_id_1 = "task-breakfast-6am"
        task_id_2 = "task-breakfast-9am"
        
        task_info = {"info": "eat breakfast"}
        
        time_6am = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0) + timedelta(days=1)
        time_9am = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
        
        # Mock Gemini response - should select task_id_1 based on user saying "6am"
        mock_call_gemini.return_value = MagicMock()
        mock_gemini_response_to_openai_like.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id_1
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query
        original_task_1 = create_mock_task(
            task_id=task_id_1,
            task_info=task_info,
            status="pending",
            time_to_execute=time_6am
        )
        original_task_2 = create_mock_task(
            task_id=task_id_2,
            task_info=task_info,
            status="pending",
            time_to_execute=time_9am
        )
        # Calls: fetch current state for both tasks, verify task_1 exists
        mock_execute_query.side_effect = [[original_task_1], [original_task_2], [original_task_1]]
        
        # Mock database update
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Create chat history with multiple tasks (same description, different times)
        tasks_data = [
            {
                "task_id": task_id_1,
                "task_info": task_info,
                "status": "pending",
                "time_to_execute": time_6am.isoformat()
            },
            {
                "task_id": task_id_2,
                "task_info": task_info,
                "status": "pending",
                "time_to_execute": time_9am.isoformat()
            }
        ]
        
        chat_history = self.create_chat_history_with_multiple_tasks(
            tasks_data=tasks_data,
            user_message="Delete the breakfast task at 6am"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task deletion should succeed")
        self.assertEqual(result_data.get("task_id"), task_id_1, "Should delete task_id_1 (6am) based on user mentioning '6am'")
        self.assertNotEqual(result_data.get("task_id"), task_id_2, "Should NOT delete task_id_2 (9am)")
    
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.gemini_response_to_openai_like')
    @patch('app.agents.tool_agents.delete_tasks_tool_agent.call_gemini')
    def test_delete_task_from_reminder_deletes_correct_task(self, mock_call_gemini, mock_gemini_response_to_openai_like, mock_execute_update, mock_execute_query):
        """Test that when a reminder is sent, the task from the reminder is deleted based on the reminder's task_id."""
        reminder_task_id = "reminder-task-1"
        other_task_id = "other-task-2"
        
        reminder_task_info = {"info": "Take my medicine"}
        other_task_info = {"info": "Call mom"}
        
        original_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        
        # Mock Gemini response - should select the reminder task_id
        mock_call_gemini.return_value = MagicMock()
        mock_gemini_response_to_openai_like.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": reminder_task_id
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called multiple times:
        # 1. Fetch current state for available_tasks - called for each task (2 tasks here)
        # 2. Verify task exists before deletion
        original_other_task = create_mock_task(
            task_id=other_task_id,
            task_info=other_task_info,
            status="pending",
            time_to_execute=original_time + timedelta(hours=2)
        )
        original_reminder_task = create_mock_task(
            task_id=reminder_task_id,
            task_info=reminder_task_info,
            status="pending",
            time_to_execute=original_time
        )
        # All calls: fetch current state for both tasks, verify reminder task exists
        mock_execute_query.side_effect = [[original_other_task], [original_reminder_task], [original_reminder_task]]
        
        # Mock database update
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Create chat history with reminder and another task
        chat_history = [
            {"role": "user", "content": "What tasks do I have?"},
            {"role": "assistant", "name": "get_tasks_tool", "content": json.dumps({
                "tasks": [
                    {
                        "task_id": other_task_id,
                        "task_info": other_task_info,
                        "status": "pending",
                        "time_to_execute": (original_time + timedelta(hours=2)).isoformat()
                    }
                ],
                "total_count": 1
            })},
            {"role": "user", "content": f'Tell the user that it is time for them to complete this task now{{"task_id": "{reminder_task_id}", "user_id": "{DEFAULT_USER_ID}", "task_info": {json.dumps(reminder_task_info)}, "time_to_execute": "{original_time.isoformat()}"}}'},
            {"role": "assistant", "content": "Hey, just a reminder to complete your task."},
            {"role": "user", "content": "Delete that task"}
        ]
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task deletion should succeed")
        self.assertEqual(result_data.get("task_id"), reminder_task_id, "Should delete the reminder task")
        self.assertNotEqual(result_data.get("task_id"), other_task_id, "Should NOT delete the other task")
        
        # Verify execute_update was called with the right task_id
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        params = call_args[0][1]
        
        # First param should be the task_id in DELETE query
        self.assertEqual(params[0], reminder_task_id, "Should delete the reminder task based on task_id in reminder message")


if __name__ == "__main__":
    unittest.main()
