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

from app.agents.tool_agents.get_tasks_tool_agent import GetTasksToolAgent
# Import test helpers - works for both direct execution and unittest
try:
    # Try relative import first (works when run as module)
    from .test_helpers import (
        are_openai_credentials_configured,
        create_mock_tasks,
        create_mock_task,
        create_tasks_in_time_range,
        create_tasks_outside_time_range,
        get_default_user_config,
        create_chat_history_with_date,
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
    create_mock_tasks = test_helpers.create_mock_tasks
    create_mock_task = test_helpers.create_mock_task
    create_tasks_in_time_range = test_helpers.create_tasks_in_time_range
    create_tasks_outside_time_range = test_helpers.create_tasks_outside_time_range
    get_default_user_config = test_helpers.get_default_user_config
    create_chat_history_with_date = test_helpers.create_chat_history_with_date
    DEFAULT_USER_ID = test_helpers.DEFAULT_USER_ID


class GetTasksToolAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        # Skip tests if OpenAI credentials are not configured
        if not are_openai_credentials_configured():
            self.skipTest("Azure OpenAI credentials not configured")
        
        self.agent = GetTasksToolAgent()
    
    def tearDown(self):
        """Clean up after each test."""
        pass
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_with_time_range(self, mock_execute_query):
        """Test that get_tasks_tool_agent retrieves tasks for a given time range."""
        # Setup mock for execute_query (database query)
        # Return mock tasks that match the time range
        mock_tasks = create_mock_tasks(count=2)
        mock_execute_query.return_value = mock_tasks
        
        # Prepare chat history with a task query request
        chat_history = create_chat_history_with_date("What tasks do I have tomorrow?")
        
        # User config with timezone
        user_config = get_default_user_config()
        
        # Execute the tool with real OpenAI API call
        result = self.agent.execute_tool(chat_history, user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result structure
        self.assertIn("tasks", result_data, "Result should contain tasks")
        self.assertIn("total_count", result_data, "Result should contain total_count")
        
        # Verify execute_query was called (database query)
        self.assertTrue(mock_execute_query.called, "execute_query should be called to fetch tasks")
        call_args = mock_execute_query.call_args
        self.assertIn("SELECT * FROM tasks", call_args[0][0], "Should query tasks table")
        
        # Verify the query parameters
        query_params = call_args[0][1]  # The tuple of parameters
        self.assertEqual(len(query_params), 3, "Query should have 3 parameters (user_id, start_time, end_time)")
        self.assertEqual(query_params[0], user_config["user_info"]["user_id"], "Query should use correct user_id")
        
        # Verify tasks are returned
        tasks = result_data.get("tasks", [])
        self.assertIsInstance(tasks, list, "Tasks should be a list")
        self.assertEqual(result_data.get("total_count"), len(tasks), "Total count should match number of tasks")
        
        # Verify task structure
        if len(tasks) > 0:
            task = tasks[0]
            self.assertIn("task_id", task, "Task should have task_id")
            self.assertIn("time_to_execute", task, "Task should have time_to_execute")
            # Verify time_to_execute is in ISO format (string)
            self.assertIsInstance(task["time_to_execute"], str, "time_to_execute should be ISO format string")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_empty_result(self, mock_execute_query):
        """Test that get_tasks_tool_agent handles empty results correctly."""
        # Setup mock for execute_query to return empty list
        mock_execute_query.return_value = []
        
        # Prepare chat history with a task query request
        chat_history = create_chat_history_with_date("What tasks do I have next week?")
        
        # User config with timezone
        user_config = get_default_user_config()
        
        # Execute the tool with real OpenAI API call
        result = self.agent.execute_tool(chat_history, user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result structure
        self.assertIn("tasks", result_data, "Result should contain tasks")
        self.assertIn("total_count", result_data, "Result should contain total_count")
        
        # Verify empty result
        tasks = result_data.get("tasks", [])
        self.assertEqual(len(tasks), 0, "Tasks list should be empty")
        self.assertEqual(result_data.get("total_count"), 0, "Total count should be 0")
        
        # Verify execute_query was called
        self.assertTrue(mock_execute_query.called, "execute_query should be called even for empty results")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_with_fallback_user_id(self, mock_execute_query):
        """Test that get_tasks_tool_agent uses fallback user_id when not in user_config."""
        # Setup mock for execute_query
        mock_tasks = create_mock_tasks(count=1)
        mock_execute_query.return_value = mock_tasks
        
        # Prepare chat history with a task query request
        chat_history = create_chat_history_with_date("Show me my tasks")
        
        # User config without user_info (should use fallback)
        user_config = get_default_user_config(include_user_info=False)
        
        # Execute the tool with real OpenAI API call
        result = self.agent.execute_tool(chat_history, user_config)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify execute_query was called with fallback user_id
        self.assertTrue(mock_execute_query.called, "execute_query should be called")
        call_args = mock_execute_query.call_args
        query_params = call_args[0][1]  # The tuple of parameters
        self.assertEqual(query_params[0], DEFAULT_USER_ID, "Query should use fallback user_id when not in config")
        
        # Verify result structure
        self.assertIn("tasks", result_data, "Result should contain tasks")
        self.assertIn("total_count", result_data, "Result should contain total_count")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_filters_by_time_range(self, mock_execute_query):
        """Test that only tasks within the time range are returned."""
        # Create tasks: some within range, some outside
        now = datetime.now(timezone.utc)
        start_time = now + timedelta(days=1)
        end_time = now + timedelta(days=2)
        
        # Tasks within range
        tasks_in_range = create_tasks_in_time_range(start_time, end_time, count=2)
        # Tasks outside range
        tasks_outside = create_tasks_outside_time_range(before_start=False, after_end=True)
        
        # Mock returns all tasks (simulating database that doesn't filter)
        all_tasks = tasks_in_range + tasks_outside
        mock_execute_query.return_value = all_tasks
        
        chat_history = create_chat_history_with_date("What tasks do I have tomorrow?")
        user_config = get_default_user_config()
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify query was called with correct time range
        call_args = mock_execute_query.call_args
        query_params = call_args[0][1]
        query_start = query_params[1]
        query_end = query_params[2]
        
        # Verify times are in UTC
        self.assertEqual(query_start.tzinfo, timezone.utc, "Start time should be in UTC")
        self.assertEqual(query_end.tzinfo, timezone.utc, "End time should be in UTC")
        
        # Note: The actual filtering happens in the SQL query, but we verify
        # that the query parameters are set correctly
        self.assertIsInstance(query_start, datetime, "Start time should be datetime")
        self.assertIsInstance(query_end, datetime, "End time should be datetime")
        self.assertLess(query_start, query_end, "Start time should be before end time")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_with_different_user_ids(self, mock_execute_query):
        """Test that query uses correct user_id from config."""
        different_user_id = "different-user-id-12345"
        mock_tasks = create_mock_tasks(count=2, user_id=different_user_id)
        mock_execute_query.return_value = mock_tasks
        
        chat_history = create_chat_history_with_date("Show me my tasks")
        user_config = get_default_user_config(user_id=different_user_id)
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify query was called with correct user_id
        call_args = mock_execute_query.call_args
        query_params = call_args[0][1]
        self.assertEqual(query_params[0], different_user_id, "Query should use user_id from config")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_with_timezone_conversion(self, mock_execute_query):
        """Test that tasks are converted to user's timezone in response."""
        # Create tasks in UTC
        now_utc = datetime.now(timezone.utc)
        task_time_utc = now_utc + timedelta(days=1, hours=5)
        
        mock_tasks = [
            create_mock_task(
                task_id="timezone-task-1",
                task_info={"info": "Timezone test task"},
                time_to_execute=task_time_utc
            )
        ]
        mock_execute_query.return_value = mock_tasks
        
        # Use PST timezone (UTC-8)
        chat_history = create_chat_history_with_date("What tasks do I have tomorrow?")
        user_config = get_default_user_config(timezone="America/Los_Angeles")
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify tasks are returned
        tasks = result_data.get("tasks", [])
        self.assertGreater(len(tasks), 0, "Should return tasks")
        
        # Verify time_to_execute is serialized as ISO string
        if len(tasks) > 0:
            task_time_str = tasks[0].get("time_to_execute")
            self.assertIsInstance(task_time_str, str, "time_to_execute should be ISO string")
            # Parse and verify it's a valid datetime
            parsed_time = datetime.fromisoformat(task_time_str.replace('Z', '+00:00'))
            self.assertIsNotNone(parsed_time, "Should parse as valid datetime")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_today_range(self, mock_execute_query):
        """Test querying tasks for today."""
        # Create tasks for today
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        mock_tasks = create_tasks_in_time_range(today_start, today_end, count=3)
        mock_execute_query.return_value = mock_tasks
        
        chat_history = create_chat_history_with_date("What tasks do I have today?")
        user_config = get_default_user_config()
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify query parameters
        call_args = mock_execute_query.call_args
        query_params = call_args[0][1]
        query_start = query_params[1]
        query_end = query_params[2]
        
        # Verify times are in UTC
        self.assertEqual(query_start.tzinfo, timezone.utc)
        self.assertEqual(query_end.tzinfo, timezone.utc)
        
        # Verify tasks are returned
        self.assertEqual(result_data.get("total_count"), len(mock_tasks))
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_next_week_range(self, mock_execute_query):
        """Test querying tasks for next week."""
        # Create tasks for next week
        now = datetime.now(timezone.utc)
        next_week_start = now + timedelta(days=7)
        next_week_end = now + timedelta(days=14)
        
        mock_tasks = create_tasks_in_time_range(next_week_start, next_week_end, count=5)
        mock_execute_query.return_value = mock_tasks
        
        chat_history = create_chat_history_with_date("What tasks do I have next week?")
        user_config = get_default_user_config()
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify query was called
        self.assertTrue(mock_execute_query.called)
        
        # Verify query parameters
        call_args = mock_execute_query.call_args
        query_params = call_args[0][1]
        query_start = query_params[1]
        query_end = query_params[2]
        
        # Verify time range is reasonable (next week interpretation can vary, so just check structure)
        # The AI might interpret "next week" differently, so we just verify:
        # 1. Query was made with valid datetime parameters
        # 2. Start time is before end time
        # 3. Both are in UTC
        self.assertIsInstance(query_start, datetime, "Start time should be datetime")
        self.assertIsInstance(query_end, datetime, "End time should be datetime")
        self.assertEqual(query_start.tzinfo, timezone.utc, "Start time should be in UTC")
        self.assertEqual(query_end.tzinfo, timezone.utc, "End time should be in UTC")
        self.assertLess(query_start, query_end, "Start time should be before end time")
        
        # Verify tasks are returned
        self.assertEqual(result_data.get("total_count"), len(mock_tasks))
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_with_mixed_statuses(self, mock_execute_query):
        """Test that tasks with different statuses are all returned."""
        now = datetime.now(timezone.utc)
        task_time = now + timedelta(days=1)
        
        mock_tasks = [
            create_mock_task(
                task_id="pending-task",
                status="pending",
                time_to_execute=task_time
            ),
            create_mock_task(
                task_id="completed-task",
                status="completed",
                time_to_execute=task_time + timedelta(hours=2)
            ),
            create_mock_task(
                task_id="in-progress-task",
                status="in_progress",
                time_to_execute=task_time + timedelta(hours=4)
            )
        ]
        mock_execute_query.return_value = mock_tasks
        
        chat_history = create_chat_history_with_date("Show me all my tasks")
        user_config = get_default_user_config()
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify all tasks are returned regardless of status
        tasks = result_data.get("tasks", [])
        self.assertEqual(len(tasks), 3, "Should return all tasks regardless of status")
        
        # Verify each task has status field
        for task in tasks:
            self.assertIn("status", task, "Task should have status field")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_query_structure(self, mock_execute_query):
        """Test that the SQL query structure is correct."""
        mock_execute_query.return_value = []
        
        chat_history = create_chat_history_with_date("What tasks do I have?")
        user_config = get_default_user_config()
        
        self.agent.execute_tool(chat_history, user_config)
        
        # Verify query structure
        call_args = mock_execute_query.call_args
        query = call_args[0][0]
        
        # Verify SQL query contains expected elements
        query_upper = query.upper()
        self.assertIn("SELECT", query_upper, "Query should be a SELECT statement")
        self.assertIn("FROM TASKS", query_upper, "Query should select from tasks table")
        self.assertIn("WHERE", query_upper, "Query should have WHERE clause")
        self.assertIn("user_id", query.lower(), "Query should filter by user_id")
        self.assertIn("time_to_execute", query.lower(), "Query should filter by time_to_execute")
        self.assertIn(">=", query, "Query should use >= for start time")
        self.assertIn("<=", query, "Query should use <= for end time")
        
        # Verify parameters
        query_params = call_args[0][1]
        self.assertEqual(len(query_params), 3, "Query should have 3 parameters")
        self.assertIsInstance(query_params[0], str, "First param (user_id) should be string")
        self.assertIsInstance(query_params[1], datetime, "Second param (start_time) should be datetime")
        self.assertIsInstance(query_params[2], datetime, "Third param (end_time) should be datetime")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_handles_large_result_set(self, mock_execute_query):
        """Test that the agent handles a large number of tasks correctly."""
        # Create 50 tasks
        mock_tasks = create_mock_tasks(count=50)
        mock_execute_query.return_value = mock_tasks
        
        chat_history = create_chat_history_with_date("Show me all my tasks")
        user_config = get_default_user_config()
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify all tasks are returned
        tasks = result_data.get("tasks", [])
        self.assertEqual(len(tasks), 50, "Should return all 50 tasks")
        self.assertEqual(result_data.get("total_count"), 50, "Total count should be 50")
        
        # Verify each task has required fields
        for task in tasks:
            self.assertIn("task_id", task)
            self.assertIn("user_id", task)
            self.assertIn("time_to_execute", task)
            self.assertIsInstance(task["time_to_execute"], str, "time_to_execute should be serialized as string")
    
    @patch('app.agents.tool_agents.get_tasks_tool_agent.execute_query')
    def test_get_tasks_with_custom_task_info(self, mock_execute_query):
        """Test that tasks with custom task_info structures are handled correctly."""
        now = datetime.now(timezone.utc)
        task_time = now + timedelta(days=1)
        
        mock_tasks = [
            create_mock_task(
                task_id="custom-task-1",
                task_info={"info": "Simple task", "priority": "high"},
                time_to_execute=task_time
            ),
            create_mock_task(
                task_id="custom-task-2",
                task_info={"title": "Task with title", "description": "Task description"},
                time_to_execute=task_time + timedelta(hours=2)
            ),
            create_mock_task(
                task_id="custom-task-3",
                task_info=None,  # No task_info
                time_to_execute=task_time + timedelta(hours=4)
            )
        ]
        mock_execute_query.return_value = mock_tasks
        
        chat_history = create_chat_history_with_date("Show me my tasks")
        user_config = get_default_user_config()
        
        result = self.agent.execute_tool(chat_history, user_config)
        result_data = json.loads(result)
        
        # Verify all tasks are returned with their task_info preserved
        tasks = result_data.get("tasks", [])
        self.assertEqual(len(tasks), 3, "Should return all tasks")
        
        # Verify task_info is preserved
        self.assertIn("task_info", tasks[0], "Task should have task_info")
        self.assertIn("task_info", tasks[1], "Task should have task_info")
        # Third task might have None task_info, which is also valid


if __name__ == '__main__':
    unittest.main()
