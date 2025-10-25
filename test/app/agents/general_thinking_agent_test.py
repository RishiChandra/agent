import unittest
import sys
import os

# Add the parent directory to the Python path to import from the main app
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from app.agents.general_thinking_agent import GeneralThinkingAgent


class GeneralThinkingAgentTest(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.agent = GeneralThinkingAgent()
    
    def test_simple_thinking(self):
        result = self.agent.think("Hello, what are my tasks today?")
        print(f"Result: {result}")

        # Verify we get a response (should be a string)
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

if __name__ == '__main__':
    unittest.main()
