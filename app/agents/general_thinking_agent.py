import logging
import asyncio

from .select_tool_agent import SelectToolAgent
import json


from .openai_client import call_openai
from .tool_agents.get_tasks_tool_agent import GetTasksToolAgent
from .tool_agents.create_tasks_tool_agent import CreateTasksToolAgent
from .tool_agents.generate_response_tool_agent import GenerateResponseToolAgent

class GeneralThinkingAgent:
    tool_agents = {}
    def __init__(self):
        agents = [GetTasksToolAgent(), CreateTasksToolAgent(), GenerateResponseToolAgent()]
        for tool_agent in agents:
            self.tool_agents[tool_agent.get_tool_name()] = tool_agent

    def think(self, user_input):
        print(f"🤔 Thinking about user input: {user_input}")
        chat_history = [{"role": "user", "content": user_input}]

        select_tool_agent = SelectToolAgent(self.tool_agents)
        
        # Tool call limits
        MAX_TOTAL_TOOL_CALLS = 10
        MAX_CONSECUTIVE_SAME_TOOL = 3
        total_tool_calls = 0
        previous_tool_name = None
        consecutive_same_tool_count = 0
        
        # Get the tool selection response
        selected_tool_response = select_tool_agent.select_tool(chat_history)
        print(f"Selected tool response: {selected_tool_response}")
        
        # Validate response structure
        if not selected_tool_response.choices or not selected_tool_response.choices[0].message.tool_calls:
            print(f"Error: No tool_calls in response. Response structure: {selected_tool_response}")
            raise ValueError(f"No tool_calls found in response. Message content: {selected_tool_response.choices[0].message.content if selected_tool_response.choices else 'No choices'}")
        
        # Extract tool name from the response
        try:
            tool_call = selected_tool_response.choices[0].message.tool_calls[0]
            print(f"Tool call function name: {tool_call.function.name}")
            print(f"Tool call arguments: {tool_call.function.arguments}")
            selected_tool_name = json.loads(tool_call.function.arguments)["tool_name"]
        except (KeyError, json.JSONDecodeError, AttributeError) as e:
            print(f"Error parsing tool call: {e}")
            print(f"Tool call structure: {tool_call if 'tool_call' in locals() else 'N/A'}")
            raise ValueError(f"Failed to parse tool name from response: {e}")
        
        # Get the actual tool agent
        if selected_tool_name not in self.tool_agents:
            # Provide helpful error message if model returned invalid tool name
            if "select_tool" in selected_tool_name.lower():
                raise ValueError(f"Invalid tool name '{selected_tool_name}'. The model incorrectly returned the selector function name. Available tools are: {list(self.tool_agents.keys())}. Please check the select_tool_agent prompt.")
            raise KeyError(f"Tool '{selected_tool_name}' not found in available tools: {list(self.tool_agents.keys())}")
        selected_tool = self.tool_agents[selected_tool_name]
        print(f"Selected tool: {selected_tool}")
        
        while selected_tool_name != "generate_response_tool":
            # Check total tool call limit
            if total_tool_calls >= MAX_TOTAL_TOOL_CALLS:
                print(f"⚠️ Maximum total tool calls ({MAX_TOTAL_TOOL_CALLS}) reached. Forcing generate_response_tool.")
                selected_tool_name = "generate_response_tool"
                selected_tool = self.tool_agents[selected_tool_name]
                break
            
            # Check consecutive same tool limit
            if selected_tool_name == previous_tool_name:
                consecutive_same_tool_count += 1
                if consecutive_same_tool_count >= MAX_CONSECUTIVE_SAME_TOOL:
                    print(f"⚠️ Maximum consecutive calls ({MAX_CONSECUTIVE_SAME_TOOL}) for tool '{selected_tool_name}' reached. Forcing generate_response_tool.")
                    selected_tool_name = "generate_response_tool"
                    selected_tool = self.tool_agents[selected_tool_name]
                    break
            else:
                consecutive_same_tool_count = 1
                previous_tool_name = selected_tool_name
            
            tool_response = selected_tool.execute_tool(chat_history)
            print(f"Tool response: {tool_response}")
            total_tool_calls += 1

            chat_history.append({"role": "assistant", "name": selected_tool.get_tool_name(), "content": tool_response})
            print(f"Chat history: {chat_history}")

            # Get next tool selection
            selected_tool_response = select_tool_agent.select_tool(chat_history)
            print(f"Selected tool response (loop): {selected_tool_response}")
            
            # Validate response structure
            if not selected_tool_response.choices or not selected_tool_response.choices[0].message.tool_calls:
                print(f"Error: No tool_calls in response. Response structure: {selected_tool_response}")
                raise ValueError(f"No tool_calls found in response. Message content: {selected_tool_response.choices[0].message.content if selected_tool_response.choices else 'No choices'}")
            
            # Extract tool name from the response
            try:
                tool_call = selected_tool_response.choices[0].message.tool_calls[0]
                print(f"Tool call function name: {tool_call.function.name}")
                print(f"Tool call arguments: {tool_call.function.arguments}")
                selected_tool_name = json.loads(tool_call.function.arguments)["tool_name"]
            except (KeyError, json.JSONDecodeError, AttributeError) as e:
                print(f"Error parsing tool call: {e}")
                print(f"Tool call structure: {tool_call if 'tool_call' in locals() else 'N/A'}")
                raise ValueError(f"Failed to parse tool name from response: {e}")
            
            if selected_tool_name not in self.tool_agents:
                # Provide helpful error message if model returned invalid tool name
                if "select_tool" in selected_tool_name.lower():
                    raise ValueError(f"Invalid tool name '{selected_tool_name}'. The model incorrectly returned the selector function name. Available tools are: {list(self.tool_agents.keys())}. Please check the select_tool_agent prompt.")
                raise KeyError(f"Tool '{selected_tool_name}' not found in available tools: {list(self.tool_agents.keys())}")
            selected_tool = self.tool_agents[selected_tool_name]
            print(f"Selected tool: {selected_tool}")

        # Final tool call will be generate response
        response = selected_tool.execute_tool(chat_history)
        return response.choices[0].message.content