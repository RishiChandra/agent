import unittest
import sys
import os
import json
from datetime import datetime, timedelta, timezone

# Add the app directory to the Python path to enable imports like "from database import ..."
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
sys.path.insert(0, os.path.join(project_root, 'app'))
sys.path.insert(0, project_root)

from app.agents.tool_agents.create_tasks_tool_agent import CreateTasksToolAgent
from app.task_crud import get_task_by_id, delete_task, get_tasks_by_user_id
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Check if Azure OpenAI credentials are configured
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_SERVICEBUS_CONNECTION_STRING = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING")


class CreateTasksToolAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        # Skip tests if OpenAI credentials are not configured
        if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
            self.skipTest("Azure OpenAI credentials not configured")
        
        self.agent = CreateTasksToolAgent()
        self.created_task_ids = []  # Track created tasks for cleanup
    
    def tearDown(self):
        """Clean up created tasks after each test."""
        for task_id in self.created_task_ids:
            try:
                delete_task(task_id)
            except Exception as e:
                print(f"Warning: Failed to delete test task {task_id}: {e}")
        self.created_task_ids.clear()
    
    def test_create_task_with_enqueue(self):
        """Test that create_tasks_tool_agent creates a task and enqueues it properly."""
        # Get today's date for context
        today = datetime.now().strftime("%Y-%m-%d")
        today_readable = datetime.now().strftime("%B %d, %Y")
        
        # Prepare chat history with a task creation request including today's date
        chat_history = [
            {"role": "user", "content": f"Today is {today_readable} ({today}). Create a task to buy groceries tomorrow at 2pm"}
        ]
        
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
        
        # Track task for cleanup
        task_id = result_data["task_id"]
        self.created_task_ids.append(task_id)
        
        # Verify task exists in database
        task = get_task_by_id(task_id)
        self.assertIsNotNone(task, "Task should exist in database")
        self.assertEqual(task["task_id"], task_id, "Task ID should match")
        self.assertEqual(task["status"], "pending", "Task status should be pending")
        
        # Verify task_info is present and contains expected content
        self.assertIsNotNone(task["task_info"], "Task info should not be None")
        if isinstance(task["task_info"], dict):
            task_info_text = task["task_info"].get("info", "")
            self.assertIn("groceries", task_info_text.lower(), "Task info should contain 'groceries'")
        
        # Verify time_to_execute is set (should be tomorrow around 2pm)
        self.assertIsNotNone(task["time_to_execute"], "Time to execute should be set")
        if task["time_to_execute"]:
            task_time = datetime.fromisoformat(task["time_to_execute"].replace('Z', '+00:00'))
            # Make tomorrow timezone-aware to match task_time
            tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
            # Allow some tolerance - should be within 24 hours
            time_diff = abs((task_time - tomorrow).total_seconds())
            self.assertLess(time_diff, 86400, "Time to execute should be approximately tomorrow")
        
        # Verify enqueue result is present (if Service Bus is configured)
        if "enqueue_result" in result_data:
            enqueue_result = result_data["enqueue_result"]
            self.assertTrue(enqueue_result.get("success"), "Enqueue should succeed")
            self.assertEqual(enqueue_result.get("task_id"), task_id, "Enqueued task ID should match")
            self.assertIn("scheduled_time", enqueue_result, "Scheduled time should be present")
            print(f"✅ Task successfully enqueued: {enqueue_result}")
        else:
            print("⚠️  Enqueue result not present (Service Bus may not be configured or enqueue failed)")

    def test_create_exactly_one_task(self):
        """Test that when user asks to create a task, exactly 1 task is created (not less or more)."""
        # Hardcoded user_id used in CreateTasksToolAgent
        user_id = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
        
        # Get today's date for context
        today = datetime.now().strftime("%Y-%m-%d")
        today_readable = datetime.now().strftime("%B %d, %Y")
        
        # Count tasks before the call
        tasks_before = get_tasks_by_user_id(user_id)
        task_count_before = len(tasks_before)
        print(f"Tasks before: {task_count_before}")
        
        # Prepare chat history with a task creation request
        chat_history = [
            {"role": "user", "content": f"Today is {today_readable} ({today}). Create a task to call mom tomorrow at 3pm"}
        ]
        
        # Execute the tool
        result = self.agent.execute_tool(chat_history)
        
        # Parse the result
        result_data = json.loads(result)
        print(f"Result data: {result_data}")
        
        # Verify the result structure
        self.assertTrue(result_data.get("success"), "Task creation should succeed")
        self.assertIn("task_id", result_data, "Result should contain task_id")
        
        # Track task for cleanup
        task_id = result_data["task_id"]
        self.created_task_ids.append(task_id)
        
        # Count tasks after the call
        tasks_after = get_tasks_by_user_id(user_id)
        task_count_after = len(tasks_after)
        print(f"Tasks after: {task_count_after}")
        
        # Verify that exactly 1 new task was created
        tasks_created = task_count_after - task_count_before
        self.assertEqual(tasks_created, 1, 
                        f"Exactly 1 task should be created. Expected 1, but {tasks_created} task(s) were created. "
                        f"Tasks before: {task_count_before}, Tasks after: {task_count_after}")
        
        # Verify the created task exists and matches the returned task_id
        created_task = get_task_by_id(task_id)
        self.assertIsNotNone(created_task, "Created task should exist in database")
        self.assertEqual(created_task["task_id"], task_id, "Task ID should match")
        self.assertEqual(created_task["status"], "pending", "Task status should be pending")


if __name__ == '__main__':
    unittest.main()

