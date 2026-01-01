import unittest
import sys
import os

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

class GeneralThinkingAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.agent = GeneralThinkingAgent()
    
    def test_simple_thinking(self):
        result = self.agent.think("Hello, what are my tasks today?", None)
        print(f"Result: {result}")

        # Verify we get a response (should be a string)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)
    
    def test_create_task(self):
        result = self.agent.think("Create a task to buy groceries tomorrow at 2pm", None)
        print(f"Result: {result}")

        # Verify we get a response (should be a string)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)
        

if __name__ == '__main__':
    unittest.main()