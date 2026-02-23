import unittest
import sys
import os
import json
from unittest.mock import Mock, patch
from types import SimpleNamespace

# Add the app directory to the Python path to enable imports like "from database import ..."
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
sys.path.insert(0, os.path.join(project_root, 'app'))
sys.path.insert(0, project_root)

from app.agents.general_thinking_agent import GeneralThinkingAgent
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Check if Azure OpenAI credentials are configured
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")


def create_mock_tool_call_response(tool_names):
    """
    Create a mock response from SelectToolAgent that selects the given tool(s).
    
    Args:
        tool_names: List of tool names to select, or a single tool name string
    
    Returns:
        Mock object that mimics the structure returned by gemini_response_to_openai_like
    """
    if isinstance(tool_names, str):
        tool_names = [tool_names]
    
    tool_calls = []
    for idx, tool_name in enumerate(tool_names):
        tool_call = SimpleNamespace(
            id=f"call_{idx}",
            function=SimpleNamespace(
                name="select_tool",
                arguments=json.dumps({"tool_name": tool_name})
            )
        )
        tool_calls.append(tool_call)
    
    message = SimpleNamespace(
        content="",
        tool_calls=tool_calls if tool_calls else None
    )
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def create_mock_tool_response(tool_name, success=True, **kwargs):
    """
    Create a mock response from a tool agent's execute_tool method.
    
    Args:
        tool_name: Name of the tool
        success: Whether the operation succeeded
        **kwargs: Additional fields to include in the response
    
    Returns:
        JSON string response
    """
    if tool_name == "get_tasks_tool":
        response = {
            "tasks": kwargs.get("tasks", []),
            "total_count": kwargs.get("total_count", 0)
        }
    elif tool_name == "create_tasks_tool":
        response = {
            "success": success,
            "status": kwargs.get("status", "all_tasks_created" if success else "failed"),
            "task_id": kwargs.get("task_id", "test_task_123"),
            "task_info": kwargs.get("task_info", {"info": "Test task"}),
            "time_to_execute": kwargs.get("time_to_execute", "2024-01-01T12:00:00Z")
        }
    elif tool_name == "edit_tasks_tool":
        response = {
            "success": success,
            "message": kwargs.get("message", "Task updated successfully" if success else "Task update failed")
        }
    elif tool_name == "delete_tasks_tool":
        response = {
            "success": success,
            "message": kwargs.get("message", "Task deleted successfully" if success else "Task deletion failed")
        }
    elif tool_name == "generate_response_tool":
        # generate_response_tool returns a plain string, not JSON
        return kwargs.get("response", "Test response")
    else:
        response = {"success": success}
    
    return json.dumps(response)


class GeneralThinkingAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.agent = GeneralThinkingAgent()
    
    def _setup_mock_tool_agents(self):
        """Helper to create mock tool agents with execute_tool methods."""
        mock_agents = {}
        for tool_name in self.agent.tool_agents.keys():
            mock_agent = Mock()
            mock_agent.get_tool_name.return_value = tool_name
            mock_agent.get_tool_description.return_value = f"Description for {tool_name}"
            mock_agent.execute_tool = Mock(return_value=create_mock_tool_response(tool_name))
            mock_agents[tool_name] = mock_agent
        return mock_agents
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_simple_query_uses_get_tasks_then_generate_response(self, mock_select_tool_agent_class):
        """Test that a simple query like 'What are my tasks?' calls get_tasks_tool once, then generate_response_tool."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # First call selects get_tasks_tool, second call selects generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("get_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("What are my tasks today?", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify get_tasks_tool was called exactly once
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 1,
                        "get_tasks_tool should be called exactly once")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify other tools were not called
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 0,
                        "create_tasks_tool should not be called")
        self.assertEqual(mock_agents["edit_tasks_tool"].execute_tool.call_count, 0,
                        "edit_tasks_tool should not be called")
        self.assertEqual(mock_agents["delete_tasks_tool"].execute_tool.call_count, 0,
                        "delete_tasks_tool should not be called")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_create_task_uses_create_tasks_then_generate_response(self, mock_select_tool_agent_class):
        """Test that creating a task calls create_tasks_tool once, then generate_response_tool."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # First call selects create_tasks_tool, second call selects generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("create_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make create_tasks_tool return success
        mock_agents["create_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "create_tasks_tool", success=True, status="all_tasks_created"
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Create a task to buy groceries tomorrow at 2pm", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify create_tasks_tool was called exactly once
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 1,
                        "create_tasks_tool should be called exactly once")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify other tools were not called
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 0,
                        "get_tasks_tool should not be called")
        self.assertEqual(mock_agents["edit_tasks_tool"].execute_tool.call_count, 0,
                        "edit_tasks_tool should not be called")
        self.assertEqual(mock_agents["delete_tasks_tool"].execute_tool.call_count, 0,
                        "delete_tasks_tool should not be called")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_edit_task_uses_edit_tasks_then_generate_response(self, mock_select_tool_agent_class):
        """Test that editing a task calls edit_tasks_tool once, then generate_response_tool."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # First call selects edit_tasks_tool, second call selects generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("edit_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make edit_tasks_tool return success
        mock_agents["edit_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "edit_tasks_tool", success=True
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Mark task 123 as complete", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify edit_tasks_tool was called exactly once
        self.assertEqual(mock_agents["edit_tasks_tool"].execute_tool.call_count, 1,
                        "edit_tasks_tool should be called exactly once")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify other tools were not called
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 0,
                        "get_tasks_tool should not be called")
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 0,
                        "create_tasks_tool should not be called")
        self.assertEqual(mock_agents["delete_tasks_tool"].execute_tool.call_count, 0,
                        "delete_tasks_tool should not be called")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_delete_task_uses_delete_tasks_then_generate_response(self, mock_select_tool_agent_class):
        """Test that deleting a task calls delete_tasks_tool once, then generate_response_tool."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # First call selects delete_tasks_tool, second call selects generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("delete_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make delete_tasks_tool return success
        mock_agents["delete_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "delete_tasks_tool", success=True
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Delete task 123", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify delete_tasks_tool was called exactly once
        self.assertEqual(mock_agents["delete_tasks_tool"].execute_tool.call_count, 1,
                        "delete_tasks_tool should be called exactly once")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify other tools were not called
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 0,
                        "get_tasks_tool should not be called")
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 0,
                        "create_tasks_tool should not be called")
        self.assertEqual(mock_agents["edit_tasks_tool"].execute_tool.call_count, 0,
                        "edit_tasks_tool should not be called")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_complex_workflow_get_then_edit_then_generate(self, mock_select_tool_agent_class):
        """Test a complex workflow: get tasks, then edit a task, then generate response."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # Sequence: get_tasks_tool -> edit_tasks_tool -> generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("get_tasks_tool"),
            create_mock_tool_call_response("edit_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make edit_tasks_tool return success
        mock_agents["edit_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "edit_tasks_tool", success=True
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Show me my tasks and mark the first one as complete", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify get_tasks_tool was called exactly once
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 1,
                        "get_tasks_tool should be called exactly once")
        
        # Verify edit_tasks_tool was called exactly once
        self.assertEqual(mock_agents["edit_tasks_tool"].execute_tool.call_count, 1,
                        "edit_tasks_tool should be called exactly once")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify other tools were not called
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 0,
                        "create_tasks_tool should not be called")
        self.assertEqual(mock_agents["delete_tasks_tool"].execute_tool.call_count, 0,
                        "delete_tasks_tool should not be called")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_multiple_create_tasks_calls(self, mock_select_tool_agent_class):
        """Test that creating multiple tasks calls create_tasks_tool multiple times."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # Sequence: create_tasks_tool -> create_tasks_tool -> generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("create_tasks_tool"),
            create_mock_tool_call_response("create_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make create_tasks_tool return success
        mock_agents["create_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "create_tasks_tool", success=True, status="all_tasks_created"
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Create a task to buy groceries and create a task to call mom", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify create_tasks_tool was called exactly twice
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 2,
                        "create_tasks_tool should be called exactly twice")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_general_query_goes_directly_to_generate_response(self, mock_select_tool_agent_class):
        """Test that a general query (not task-related) goes directly to generate_response_tool."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # Directly select generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Hello, how are you?", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify no other tools were called
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 0,
                        "get_tasks_tool should not be called")
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 0,
                        "create_tasks_tool should not be called")
        self.assertEqual(mock_agents["edit_tasks_tool"].execute_tool.call_count, 0,
                        "edit_tasks_tool should not be called")
        self.assertEqual(mock_agents["delete_tasks_tool"].execute_tool.call_count, 0,
                        "delete_tasks_tool should not be called")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_get_then_delete_then_generate(self, mock_select_tool_agent_class):
        """Test workflow: get tasks, then delete a task, then generate response."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # Sequence: get_tasks_tool -> delete_tasks_tool -> generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("get_tasks_tool"),
            create_mock_tool_call_response("delete_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make delete_tasks_tool return success
        mock_agents["delete_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "delete_tasks_tool", success=True
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Show me my tasks and delete the first one", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify get_tasks_tool was called exactly once
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 1,
                        "get_tasks_tool should be called exactly once")
        
        # Verify delete_tasks_tool was called exactly once
        self.assertEqual(mock_agents["delete_tasks_tool"].execute_tool.call_count, 1,
                        "delete_tasks_tool should be called exactly once")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify other tools were not called
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 0,
                        "create_tasks_tool should not be called")
        self.assertEqual(mock_agents["edit_tasks_tool"].execute_tool.call_count, 0,
                        "edit_tasks_tool should not be called")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_get_tasks_then_generate_response(self, mock_select_tool_agent_class):
        """Test that get_tasks_tool is called, then select_tool is called again to get generate_response_tool."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # First call selects get_tasks_tool, second call selects generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response("get_tasks_tool"),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make get_tasks_tool return valid response with tasks
        mock_agents["get_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "get_tasks_tool", tasks=[{"id": 1, "description": "Test task"}], total_count=1
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("What are my tasks?", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify get_tasks_tool was called exactly once
        self.assertEqual(mock_agents["get_tasks_tool"].execute_tool.call_count, 1,
                        "get_tasks_tool should be called exactly once")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify select_tool was called twice (once for get_tasks, once for generate_response)
        self.assertEqual(mock_select_tool_agent.select_tool.call_count, 2,
                        "select_tool should be called twice (once for get_tasks, once for generate_response)")
    
    @patch('app.agents.general_thinking_agent.SelectToolAgent')
    def test_multiple_tool_calls_in_single_response(self, mock_select_tool_agent_class):
        """Test that multiple tool calls in a single select_tool response are all executed."""
        # Setup mocks
        mock_select_tool_agent = Mock()
        mock_select_tool_agent_class.return_value = mock_select_tool_agent
        
        # Single response with multiple tool calls: create_tasks_tool twice, then generate_response_tool
        mock_select_tool_agent.select_tool.side_effect = [
            create_mock_tool_call_response(["create_tasks_tool", "create_tasks_tool"]),
            create_mock_tool_call_response("generate_response_tool")
        ]
        
        mock_agents = self._setup_mock_tool_agents()
        # Make create_tasks_tool return success
        mock_agents["create_tasks_tool"].execute_tool.return_value = create_mock_tool_response(
            "create_tasks_tool", success=True, status="all_tasks_created"
        )
        self.agent.tool_agents = mock_agents
        
        # Execute
        result = self.agent.think("Create a task to buy groceries and create a task to call mom", None)
        
        # Verify
        self.assertIsInstance(result, dict)
        self.assertIn("result", result)
        
        # Verify create_tasks_tool was called exactly twice (from single response with 2 tool calls)
        self.assertEqual(mock_agents["create_tasks_tool"].execute_tool.call_count, 2,
                        "create_tasks_tool should be called exactly twice from single response")
        
        # Verify generate_response_tool was called exactly once
        self.assertEqual(mock_agents["generate_response_tool"].execute_tool.call_count, 1,
                        "generate_response_tool should be called exactly once")
        
        # Verify select_tool was called twice (first with 2 tool calls, second for generate_response)
        self.assertEqual(mock_select_tool_agent.select_tool.call_count, 2,
                        "select_tool should be called twice")
        

if __name__ == '__main__':
    unittest.main()
