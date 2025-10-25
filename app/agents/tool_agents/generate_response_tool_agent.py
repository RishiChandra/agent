from ..openai_client import call_openai

class GenerateResponseToolAgent:
    name = "generate_response_tool"
    description = "A tool to generate the assistants response to the user. This tool will be the final tool that is called before the response is sent to the user."

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history):
        """    
        Args:
            chat_history: list of messages in the chat history
        
        Returns:
            str: JSON string containing the assistants response to the user
        """
        messages = [
            {"role": "system", "content": f"Given the chat history {chat_history}, generate the assistants response to the user."},
        ]

        return call_openai(messages)