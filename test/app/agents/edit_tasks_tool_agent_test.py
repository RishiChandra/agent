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

from app.agents.tool_agents.edit_tasks_tool_agent import EditTasksToolAgent

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


class EditTasksToolAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        # Skip tests if OpenAI credentials are not configured
        if not are_openai_credentials_configured():
            self.skipTest("Azure OpenAI credentials not configured")
        
        self.agent = EditTasksToolAgent()
        # Use UTC for tests to avoid tzdata dependency issues
        self.user_config = get_default_user_config(timezone="UTC")
    
    def tearDown(self):
        """Clean up after each test."""
        pass
    
    def create_chat_history_with_task(self, task_id, task_info, status="pending", time_to_execute=None, user_message="I completed that task"):
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
    
    def create_chat_history_with_multiple_tasks(self, tasks_data, user_message="I completed the first task"):
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
    
    def create_chat_history_with_reminder(self, task_id, task_info, time_to_execute, user_message="I can't do that right now"):
        """Helper to create chat history with a task reminder."""
        reminder_message = f'Tell the user that it is time for them to complete this task now{{"task_id": "{task_id}", "user_id": "{DEFAULT_USER_ID}", "task_info": {json.dumps(task_info)}, "time_to_execute": "{time_to_execute}"}}'
        
        return [
            {"role": "user", "content": reminder_message},
            {"role": "assistant", "content": "Hey, just a reminder to complete your task."},
            {"role": "user", "content": user_message}
        ]
    
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.call_openai')
    def test_edit_task_status_to_completed(self, mock_call_openai, mock_execute_update, mock_execute_query):
        """Test that edit_tasks_tool_agent can mark a task as completed."""
        task_id = "test-task-1"
        task_info = {"info": "Take my medicine"}
        original_time = datetime.now(timezone.utc) + timedelta(hours=1)
        
        # Mock OpenAI response for tool selection (extracting task_id, status)
        mock_call_openai.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id,
                                "status": "completed"
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called 3 times:
        # 1. Fetch current state for available_tasks (line 169)
        # 2. Fetch task to verify it exists (line 385)
        # 3. Fetch updated task after update (line 475)
        original_task = create_mock_task(
            task_id=task_id,
            task_info=task_info,
            status="pending",
            time_to_execute=original_time
        )
        updated_task = create_mock_task(
            task_id=task_id,
            task_info=task_info,
            status="completed",  # Updated status
            time_to_execute=original_time
        )
        # All 3 calls: fetch current state, verify exists, fetch updated state
        mock_execute_query.side_effect = [[original_task], [original_task], [updated_task]]
        
        # Mock database update
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Create chat history with task
        chat_history = self.create_chat_history_with_task(
            task_id=task_id,
            task_info=task_info,
            status="pending",
            time_to_execute=original_time,
            user_message="I completed that task"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task update should succeed")
        self.assertEqual(result_data.get("task_id"), task_id, "Task ID should match")
        self.assertEqual(result_data.get("status"), "completed", "Status should be completed")
        
        # Verify execute_update was called
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        
        # Verify the update query includes status
        self.assertIn("status", query.lower(), "Update query should include status")
        self.assertEqual(params[0], "completed", "Status should be set to 'completed'")
        self.assertEqual(params[-1], task_id, "Last param should be task_id")
    
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.call_openai')
    def test_edit_task_defer_by_5_minutes(self, mock_call_openai, mock_execute_update, mock_execute_query):
        """Test that edit_tasks_tool_agent can defer a task by 5 minutes."""
        task_id = "test-task-1"
        task_info = {"info": "Take my medicine"}
        original_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        
        # Calculate expected deferred time (5 minutes later)
        expected_time = original_time + timedelta(minutes=5)
        
        # Mock OpenAI response for tool selection (extracting task_id, time_to_execute)
        # Use UTC for test to avoid tzdata dependency
        expected_time_str = expected_time.isoformat()
        
        mock_call_openai.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id,
                                "time_to_execute": expected_time_str
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called 3 times:
        # 1. Fetch current state for available_tasks (line 169)
        # 2. Fetch task to verify it exists (line 385)
        # 3. Fetch updated task after update (line 475)
        original_task = create_mock_task(
            task_id=task_id,
            task_info=task_info,
            status="pending",
            time_to_execute=original_time
        )
        updated_task = create_mock_task(
            task_id=task_id,
            task_info=task_info,
            status="pending",
            time_to_execute=expected_time  # Updated time
        )
        # All 3 calls: fetch current state, verify exists, fetch updated state
        mock_execute_query.side_effect = [[original_task], [original_task], [updated_task]]
        
        # Mock database update
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Create chat history with task reminder
        chat_history = self.create_chat_history_with_reminder(
            task_id=task_id,
            task_info=task_info,
            time_to_execute=original_time.isoformat(),
            user_message="I can't do that right now"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task update should succeed")
        self.assertEqual(result_data.get("task_id"), task_id, "Task ID should match")
        
        # Verify execute_update was called
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        
        # Verify the update query includes time_to_execute
        self.assertIn("time_to_execute", query.lower(), "Update query should include time_to_execute")
        
        # Verify the time was updated (should be approximately 5 minutes later)
        updated_time = params[0] if "time_to_execute" in query.lower() else None
        if updated_time:
            time_diff = (updated_time - original_time).total_seconds()
            # Allow 1 second tolerance
            self.assertAlmostEqual(time_diff, 300, delta=1, msg="Time should be deferred by 5 minutes (300 seconds)")
    
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.call_openai')
    def test_edit_task_description(self, mock_call_openai, mock_execute_update, mock_execute_query):
        """Test that edit_tasks_tool_agent can update task description."""
        task_id = "test-task-1"
        original_task_info = {"info": "Take my medicine"}
        new_task_info = "Take my vitamins"
        original_time = datetime.now(timezone.utc) + timedelta(hours=1)
        
        # Mock OpenAI response for tool selection (extracting task_id, task_info)
        mock_call_openai.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id,
                                "task_info": new_task_info
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called 3 times:
        # 1. Fetch current state for available_tasks (line 169)
        # 2. Fetch task to verify it exists (line 385)
        # 3. Fetch updated task after update (line 475)
        original_task = create_mock_task(
            task_id=task_id,
            task_info=original_task_info,
            status="pending",
            time_to_execute=original_time
        )
        updated_task = create_mock_task(
            task_id=task_id,
            task_info={"info": new_task_info},  # Updated task info
            status="pending",
            time_to_execute=original_time
        )
        # All 3 calls: fetch current state, verify exists, fetch updated state
        mock_execute_query.side_effect = [[original_task], [original_task], [updated_task]]
        
        # Mock database update
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Create chat history with task
        chat_history = self.create_chat_history_with_task(
            task_id=task_id,
            task_info=original_task_info,
            status="pending",
            time_to_execute=original_time,
            user_message="Change the task description to 'Take my vitamins'"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task update should succeed")
        self.assertEqual(result_data.get("task_id"), task_id, "Task ID should match")
        self.assertEqual(result_data.get("task_info", {}).get("info"), new_task_info, "Task info should be updated")
        
        # Verify execute_update was called
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        query = call_args[0][0]
        params = call_args[0][1]
        
        # Verify the update query includes task_info
        self.assertIn("task_info", query.lower(), "Update query should include task_info")
    
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.call_openai')
    def test_edit_task_only_updates_correct_task_id(self, mock_call_openai, mock_execute_update, mock_execute_query):
        """Test that edit_tasks_tool_agent only updates the task that matches the user's request."""
        task_id_1 = "task-medicine"
        task_id_2 = "task-call"
        
        task_info_1 = {"info": "Take my medicine"}
        task_info_2 = {"info": "Call mom"}
        
        original_time = datetime.now(timezone.utc) + timedelta(hours=1)
        
        # Mock OpenAI response - should select task_id_1 based on user saying "taking my medicine"
        mock_call_openai.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id_1,
                                "status": "completed"
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called 3 times:
        # 1. Fetch current state for available_tasks (line 169) - called for each task (2 tasks here)
        # 2. Fetch task to verify it exists (line 385)
        # 3. Fetch updated task after update (line 475)
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
            time_to_execute=original_time
        )
        updated_task = create_mock_task(
            task_id=task_id_1,
            task_info=task_info_1,
            status="completed",  # Updated status
            time_to_execute=original_time
        )
        # All calls: fetch current state for both tasks, verify exists, fetch updated state
        mock_execute_query.side_effect = [[original_task_1], [original_task_2], [original_task_1], [updated_task]]
        
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
            user_message="I completed taking my medicine"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task update should succeed")
        self.assertEqual(result_data.get("task_id"), task_id_1, "Should update task_id_1 based on user's mention of 'taking my medicine'")
        self.assertNotEqual(result_data.get("task_id"), task_id_2, "Should NOT update task_id_2 (Call mom)")
        
        # Verify execute_update was called with the right task_id
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        params = call_args[0][1]
        
        # Last param should be the task_id in WHERE clause
        self.assertEqual(params[-1], task_id_1, "Should update task_id_1 based on user's request")
        self.assertNotEqual(params[-1], task_id_2, "Should NOT update task_id_2")
    
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.call_openai')
    def test_edit_task_with_multiple_tasks_selects_correct_one(self, mock_call_openai, mock_execute_update, mock_execute_query):
        """Test that when multiple tasks are in chat history, the right one is selected based on user's mention."""
        task_id_1 = "task-medicine"
        task_id_2 = "task-call"
        task_id_3 = "task-groceries"
        
        task_info_1 = {"info": "Take my medicine"}
        task_info_2 = {"info": "Call mom"}
        task_info_3 = {"info": "Buy groceries"}
        
        original_time = datetime.now(timezone.utc) + timedelta(hours=1)
        
        # Mock OpenAI response - should select task_id_2 based on user mentioning "call"
        mock_call_openai.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": task_id_2,
                                "status": "completed"
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called multiple times:
        # 1. Fetch current state for available_tasks (line 169) - called for each task (3 tasks here)
        # 2. Fetch task to verify it exists (line 385)
        # 3. Fetch updated task after update (line 475)
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
            time_to_execute=original_time
        )
        original_task_3 = create_mock_task(
            task_id=task_id_3,
            task_info=task_info_3,
            status="pending",
            time_to_execute=original_time + timedelta(hours=2)
        )
        updated_task = create_mock_task(
            task_id=task_id_2,
            task_info=task_info_2,
            status="completed",  # Updated status
            time_to_execute=original_time
        )
        # All calls: fetch current state for all 3 tasks, verify exists, fetch updated state
        mock_execute_query.side_effect = [[original_task_1], [original_task_2], [original_task_3], [original_task_2], [updated_task]]
        
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
                "time_to_execute": (original_time + timedelta(hours=1)).isoformat()
            },
            {
                "task_id": task_id_3,
                "task_info": task_info_3,
                "status": "pending",
                "time_to_execute": (original_time + timedelta(hours=2)).isoformat()
            }
        ]
        
        chat_history = self.create_chat_history_with_multiple_tasks(
            tasks_data=tasks_data,
            user_message="I completed the call to mom"
        )
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task update should succeed")
        self.assertEqual(result_data.get("task_id"), task_id_2, "Should update task_id_2 (call mom)")
        self.assertNotEqual(result_data.get("task_id"), task_id_1, "Should NOT update task_id_1")
        self.assertNotEqual(result_data.get("task_id"), task_id_3, "Should NOT update task_id_3")
        
        # Verify execute_update was called with the right task_id
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        params = call_args[0][1]
        
        # Last param should be the task_id in WHERE clause
        self.assertEqual(params[-1], task_id_2, "Should update task_id_2 based on user's mention of 'call to mom'")
    
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.call_openai')
    def test_edit_task_fails_without_task_id(self, mock_call_openai, mock_execute_query):
        """Test that edit_tasks_tool_agent returns an error when no task_id is available."""
        # Mock OpenAI response - no task_id available
        mock_call_openai.return_value = MagicMock(
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
        
        # Mock database query - return empty (task not found)
        # Note: execute_query is called twice - once to fetch from DB for available_tasks, once after update
        # But if task_id is not in available_tasks, it returns early, so we only need to mock once
        mock_execute_query.return_value = []
        
        # Create chat history without any tasks
        chat_history = [
            {"role": "user", "content": "I completed a task"}
        ]
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result indicates failure
        self.assertFalse(result_data.get("success", True), "Task update should fail without task_id")
        message = result_data.get("message", "").lower()
        self.assertTrue(
            "task_id" in message or "no task" in message or "not found" in message,
            f"Error message should indicate task_id issue. Got: {message}"
        )
    
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_query')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.execute_update')
    @patch('app.agents.tool_agents.edit_tasks_tool_agent.call_openai')
    def test_edit_task_from_reminder_updates_correct_task(self, mock_call_openai, mock_execute_update, mock_execute_query):
        """Test that when a reminder is sent, the task from the reminder is updated based on the reminder's task_id."""
        reminder_task_id = "reminder-task-1"
        other_task_id = "other-task-2"
        
        reminder_task_info = {"info": "Take my medicine"}
        other_task_info = {"info": "Call mom"}
        
        original_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        expected_time = original_time + timedelta(minutes=5)
        
        # Mock OpenAI response - should select the reminder task_id
        # Use UTC for test to avoid tzdata dependency
        expected_time_str = expected_time.isoformat()
        
        mock_call_openai.return_value = MagicMock(
            choices=[MagicMock(
                message=MagicMock(
                    tool_calls=[MagicMock(
                        function=MagicMock(
                            arguments=json.dumps({
                                "task_id": reminder_task_id,
                                "time_to_execute": expected_time_str
                            })
                        )
                    )]
                )
            )]
        )
        
        # Mock database query - called multiple times:
        # 1. Fetch current state for available_tasks (line 169) - called for each task (2 tasks here)
        # 2. Fetch task to verify it exists (line 385)
        # 3. Fetch updated task after update (line 475)
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
        updated_task = create_mock_task(
            task_id=reminder_task_id,
            task_info=reminder_task_info,
            status="pending",
            time_to_execute=expected_time  # Updated time
        )
        # All calls: fetch current state for both tasks, verify exists, fetch updated state
        mock_execute_query.side_effect = [[original_other_task], [original_reminder_task], [original_reminder_task], [updated_task]]
        
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
            {"role": "user", "content": "I can't do that right now"}
        ]
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history, self.user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result
        self.assertTrue(result_data.get("success"), "Task update should succeed")
        self.assertEqual(result_data.get("task_id"), reminder_task_id, "Should update the reminder task")
        self.assertNotEqual(result_data.get("task_id"), other_task_id, "Should NOT update the other task")
        
        # Verify execute_update was called with the right task_id
        self.assertTrue(mock_execute_update.called, "execute_update should be called")
        call_args = mock_execute_update.call_args
        params = call_args[0][1]
        
        # Last param should be the task_id in WHERE clause
        self.assertEqual(params[-1], reminder_task_id, "Should update the reminder task based on task_id in reminder message")


if __name__ == "__main__":
    unittest.main()
