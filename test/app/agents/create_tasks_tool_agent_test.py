import unittest
import sys
import os
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Add the app directory to the Python path to enable imports like "from database import ..."
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
test_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, os.path.join(project_root, 'app'))
sys.path.insert(0, project_root)
sys.path.insert(0, test_dir)

from app.agents.tool_agents.create_tasks_tool_agent import CreateTasksToolAgent
# Import test helpers - works for both direct execution and unittest
try:
    # Try relative import first (works when run as module)
    from .test_helpers import (
        are_openai_credentials_configured,
        create_enqueue_side_effect,
        create_chat_history_with_date
    )
except ImportError:
    # Fall back to direct import (works when run as script)
    import importlib.util
    helpers_path = os.path.join(os.path.dirname(__file__), 'test_helpers.py')
    spec = importlib.util.spec_from_file_location("test_helpers", helpers_path)
    test_helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(test_helpers)
    are_openai_credentials_configured = test_helpers.are_openai_credentials_configured
    create_enqueue_side_effect = test_helpers.create_enqueue_side_effect
    create_chat_history_with_date = test_helpers.create_chat_history_with_date


class CreateTasksToolAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        # Skip tests if OpenAI credentials are not configured
        if not are_openai_credentials_configured():
            self.skipTest("Azure OpenAI credentials not configured")
        
        self.agent = CreateTasksToolAgent()
    
    def tearDown(self):
        """Clean up after each test."""
        pass
    
    @patch('app.agents.tool_agents.create_tasks_tool_agent.enqueue_task')
    @patch('app.agents.tool_agents.create_tasks_tool_agent.execute_update')
    def test_create_task_with_enqueue(self, mock_execute_update, mock_enqueue_task):
        """Test that create_tasks_tool_agent creates a task and enqueues it properly."""
        # Setup mock for execute_update (database insert)
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Setup mock for enqueue_task (Service Bus)
        mock_enqueue_task.side_effect = create_enqueue_side_effect()
        
        # Prepare chat history with a task creation request including today's date
        chat_history = create_chat_history_with_date("Create a task to buy groceries tomorrow at 2pm")
        
        # Execute the tool with real OpenAI API call
        result = self.agent.execute_tool(chat_history)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result structure
        self.assertTrue(result_data.get("success"), "Task creation should succeed")
        self.assertIn("task_id", result_data, "Result should contain task_id")
        self.assertIn("task_info", result_data, "Result should contain task_info")
        self.assertIn("time_to_execute", result_data, "Result should contain time_to_execute")
        
        task_id = result_data["task_id"]
        
        # Verify execute_update was called (database insert)
        self.assertTrue(mock_execute_update.called, "execute_update should be called to insert task")
        call_args = mock_execute_update.call_args
        self.assertIn("INSERT INTO tasks", call_args[0][0], "Should insert into tasks table")
        
        # Verify task_info is present and contains expected content
        task_info = result_data.get("task_info")
        self.assertIsNotNone(task_info, "Task info should not be None")
        if isinstance(task_info, dict):
            task_info_text = task_info.get("info", "")
            self.assertIn("groceries", task_info_text.lower(), "Task info should contain 'groceries'")
        
        # Verify time_to_execute is set (should be tomorrow around 2pm)
        time_to_execute = result_data.get("time_to_execute")
        self.assertIsNotNone(time_to_execute, "Time to execute should be set")
        if time_to_execute:
            task_time = datetime.fromisoformat(time_to_execute.replace('Z', '+00:00'))
            # Make tomorrow timezone-aware to match task_time
            tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
            # Allow some tolerance - should be within 24 hours
            time_diff = abs((task_time - tomorrow).total_seconds())
            self.assertLess(time_diff, 86400, "Time to execute should be approximately tomorrow")
        
        # Verify enqueue was called
        self.assertTrue(mock_enqueue_task.called, "enqueue_task should be called")
        enqueue_call_args = mock_enqueue_task.call_args
        self.assertEqual(enqueue_call_args[1]["task_id"], task_id, "Enqueue should be called with correct task_id")
        
        # Verify enqueue result is present
        if "enqueue_result" in result_data:
            enqueue_result = result_data["enqueue_result"]
            self.assertTrue(enqueue_result.get("success"), "Enqueue should succeed")
            self.assertEqual(enqueue_result.get("task_id"), task_id, "Enqueued task ID should match")
            self.assertIn("scheduled_time", enqueue_result, "Scheduled time should be present")
            print(f"✅ Task successfully enqueued: {enqueue_result}")
        else:
            print("⚠️  Enqueue result not present (Service Bus may not be configured or enqueue failed)")

    @patch('app.agents.tool_agents.create_tasks_tool_agent.enqueue_task')
    @patch('app.agents.tool_agents.create_tasks_tool_agent.execute_update')
    def test_create_exactly_one_task(self, mock_execute_update, mock_enqueue_task):
        """Test that when user asks to create a task, exactly 1 task is created (not less or more)."""
        # Setup mock for execute_update (database insert)
        # Track how many times it's called
        mock_execute_update.return_value = 1  # 1 row affected
        
        # Setup mock for enqueue_task (Service Bus)
        mock_enqueue_task.side_effect = create_enqueue_side_effect()
        
        # Prepare chat history with a task creation request
        chat_history = create_chat_history_with_date("Create a task to call mom tomorrow at 3pm")
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result structure
        self.assertTrue(result_data.get("success"), "Task creation should succeed")
        self.assertIn("task_id", result_data, "Result should contain task_id")
        
        task_id = result_data["task_id"]
        
        # Verify execute_update was called exactly once
        self.assertEqual(mock_execute_update.call_count, 1, 
                        f"execute_update should be called exactly once. Called {mock_execute_update.call_count} time(s).")
        
        # Verify the call was for an INSERT
        call_args = mock_execute_update.call_args
        self.assertIn("INSERT INTO tasks", call_args[0][0], "Should insert into tasks table")
        
        # Verify that exactly 1 task creation was attempted
        # Since we're mocking, we verify by checking the number of calls to execute_update
        tasks_created = mock_execute_update.call_count
        self.assertEqual(tasks_created, 1, 
                        f"Exactly 1 task should be created. Expected 1, but {tasks_created} task(s) were created.")
        
        # Verify enqueue was called exactly once
        self.assertEqual(mock_enqueue_task.call_count, 1,
                        f"enqueue_task should be called exactly once. Called {mock_enqueue_task.call_count} time(s).")
        
        # Verify the task_id in the enqueue call matches
        enqueue_call_args = mock_enqueue_task.call_args
        self.assertEqual(enqueue_call_args[1]["task_id"], task_id, "Enqueue should be called with correct task_id")


if __name__ == '__main__':
    unittest.main()

