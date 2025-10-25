import logging
import asyncio

from .select_tool_agent import SelectToolAgent
import json


from .openai_client import call_openai
from .tool_agents.get_tasks_tool_agent import GetTasksToolAgent
from .tool_agents.generate_response_tool_agent import GenerateResponseToolAgent

class GeneralThinkingAgent:
    tool_agents = {}
    def __init__(self):
        agents = [GetTasksToolAgent(), GenerateResponseToolAgent()]
        for tool_agent in agents:
            self.tool_agents[tool_agent.get_tool_name()] = tool_agent

    def think(self, user_input):
        print(f"🤔 Thinking about user input: {user_input}")
        chat_history = [{"role": "user", "content": user_input}]

        select_tool_agent = SelectToolAgent(self.tool_agents)
        
        # Get the tool selection response
        selected_tool_response = select_tool_agent.select_tool(chat_history)
        print(f"Selected tool response: {selected_tool_response}")
        
        # Extract tool name from the response
        selected_tool_name = json.loads(selected_tool_response.choices[0].message.tool_calls[0].function.arguments)["tool_name"]
        
        # Get the actual tool agent
        selected_tool = self.tool_agents[selected_tool_name]
        print(f"Selected tool: {selected_tool}")
        
        while selected_tool_name != "generate_response_tool":
            tool_response = selected_tool.execute_tool(chat_history)
            print(f"Tool response: {tool_response}")

            chat_history.append({"role": "assistant", "name": selected_tool.get_tool_name(), "content": tool_response})
            print(f"Chat history: {chat_history}")

            # Get next tool selection
            selected_tool_response = select_tool_agent.select_tool(chat_history)
            selected_tool_name = json.loads(selected_tool_response.choices[0].message.tool_calls[0].function.arguments)["tool_name"]
            selected_tool = self.tool_agents[selected_tool_name]
            print(f"Selected tool: {selected_tool}")

        # Final tool call will be generate response
        response = selected_tool.execute_tool(chat_history)
        return response.choices[0].message.content