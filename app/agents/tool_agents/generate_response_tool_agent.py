import json
from ..openai_client import call_openai

class GenerateResponseToolAgent:
    name = "generate_response_tool"
    description = "A tool to generate the assistants response to the user. This tool will be the final tool that is called before the response is sent to the user."

    def get_tool_description(self):
        return self.description

    def get_tool_name(self):
        return self.name

    def execute_tool(self, chat_history, user_config=None):
        """    
        Args:
            chat_history: list of messages in the chat history
            user_config: UserConfigData containing user information (timezone, date, time, etc.)
        
        Returns:
            str: JSON string containing the assistants response to the user
        """
        system_content = (
            "Given the chat history, generate the assistant's response to the user. "
            "CRITICAL ANTI-HALLUCINATION RULE: You must base your response EXCLUSIVELY on the information provided in the chat history. "
            "You are FORBIDDEN from making up, inventing, adding, or mentioning any tasks, events, meetings, deadlines, or information "
            "that is NOT explicitly mentioned in the chat history. "
            "If the chat history contains task data from get_tasks_tool, you MUST use ONLY that exact data - do NOT add, infer, or create "
            "additional tasks or details that weren't in the tool response.\n\n"
            "ZERO TOLERANCE FOR HALLUCINATION: If the tool response lists specific tasks like 'take my medicine' and 'brush my teeth', "
            "you MUST ONLY mention those exact tasks. You MUST NOT mention ANY other tasks that are not in the tool response. This is a critical error.\n\n"
            "CRITICAL: If multiple tasks were created (you'll see multiple 'create_tasks_tool' responses with 'success': true), "
            "you MUST mention ALL of them in your response. Do not skip any tasks that were successfully created. "
            "List each task with its description and scheduled time. But do NOT add any tasks that weren't in the tool responses.\n\n"
            
        )
        
        # Add timezone context if available
        if user_config:
            timezone = user_config.get("timezone", "UTC")
            system_content += f" When mentioning times, use the user's timezone ({timezone}). Times in the chat history are already in the user's timezone."
        
        # Build messages with system prompt and chat history
        messages = [
            {"role": "system", "content": system_content},
        ]
        
        # Add the chat history to the messages
        # Ensure all message content is a string (not an object)
        cleaned_chat_history = []
        for msg in chat_history:
            cleaned_msg = dict(msg)  # Make a copy
            # If content is not a string, convert it to string
            if isinstance(cleaned_msg.get("content"), dict):
                cleaned_msg["content"] = json.dumps(cleaned_msg["content"])
            elif not isinstance(cleaned_msg.get("content"), str):
                cleaned_msg["content"] = str(cleaned_msg.get("content", ""))
            cleaned_chat_history.append(cleaned_msg)
        
        messages.extend(cleaned_chat_history)

        response = call_openai(messages)
        # Return the content string, not the ChatCompletion object
        return response.choices[0].message.content