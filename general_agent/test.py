import os
from dotenv import load_dotenv

# Load environment variables from .env file BEFORE importing general_agent
load_dotenv()

# Now import general_agent after environment variables are loaded
from general_agent.general_agent import call_openai

# Test the OpenAI call
response = call_openai("Hi")
if response:
    print(f"Response: {response}")
else:
    print("Failed to get response from OpenAI")
