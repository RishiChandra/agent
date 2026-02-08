import logging
import asyncio

from .select_tool_agent import SelectToolAgent
import json


from .openai_client import call_openai
from .tool_agents.get_tasks_tool_agent import GetTasksToolAgent
from .tool_agents.create_tasks_tool_agent import CreateTasksToolAgent
from .tool_agents.edit_tasks_tool_agent import EditTasksToolAgent
from .tool_agents.delete_tasks_tool_agent import DeleteTasksToolAgent
from .tool_agents.generate_response_tool_agent import GenerateResponseToolAgent
from .utils.scratchpad_utils import check_if_already_processed, build_chat_history_from_scratchpad

class GeneralThinkingAgent:
    tool_agents = {}
    def __init__(self):
        agents = [GetTasksToolAgent(), CreateTasksToolAgent(), EditTasksToolAgent(), DeleteTasksToolAgent(), GenerateResponseToolAgent()]
        for tool_agent in agents:
            self.tool_agents[tool_agent.get_tool_name()] = tool_agent

    def _extract_tool_names_from_response(self, selected_tool_response, context=""):
        """
        Extract tool names from a tool selection response, handling single or multiple tool calls uniformly.
        
        Args:
            selected_tool_response: The response from select_tool_agent
            context: Optional context string for logging (e.g., "loop", "initial")
        
        Returns:
            list: List of tool names to execute
        """
        # Validate response structure
        if not selected_tool_response.choices or not selected_tool_response.choices[0].message.tool_calls:
            context_msg = f" in {context}" if context else ""
            print(f"Error: No tool_calls in response{context_msg}. Response structure: {selected_tool_response}")
            raise ValueError(f"No tool_calls found in response{context_msg}. Message content: {selected_tool_response.choices[0].message.content if selected_tool_response.choices else 'No choices'}")
        
        # Extract all tool names uniformly
        try:
            tool_calls = selected_tool_response.choices[0].message.tool_calls
            tool_names = []
            
            for idx, tool_call in enumerate(tool_calls):
                tool_name = json.loads(tool_call.function.arguments)["tool_name"]
                if tool_name not in self.tool_agents:
                    print(f"⚠️ Skipping invalid tool '{tool_name}' in tool call response")
                    continue
                tool_names.append(tool_name)
            
            if len(tool_names) > 1:
                context_msg = f" ({context})" if context else ""
                print(f"⚠️ Multiple tool calls detected ({len(tool_names)}){context_msg}: {tool_names}")
            else:
                print(f"Tool call function name: {tool_calls[0].function.name}")
                print(f"Tool call arguments: {tool_calls[0].function.arguments}")
                print(f"Selected tool: {tool_names[0] if tool_names else 'N/A'}")
            
            if not tool_names:
                raise ValueError("No valid tool names found in response")
            
            return tool_names
            
        except (KeyError, json.JSONDecodeError, AttributeError) as e:
            print(f"Error parsing tool call: {e}")
            print(f"Tool call structure: {tool_call if 'tool_call' in locals() else 'N/A'}")
            raise ValueError(f"Failed to parse tool name from response: {e}")

    def _should_short_circuit_to_generate_response(self, tool_name, tool_response):
        """
        Check if we should short-circuit to generate_response_tool after executing a tool.
        
        Args:
            tool_name: Name of the tool that was just executed
            tool_response: Response from the tool execution
        
        Returns:
            bool: True if we should short-circuit to generate_response_tool
        """
        if tool_name == "get_tasks_tool":
            try:
                parsed = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
                # get_tasks_tool always returns a valid response (even if empty)
                # If we see a response with "tasks" key, it's valid and we should generate a response
                if parsed and ("tasks" in parsed or "total_count" in parsed):
                    return True
            except Exception as e:
                print(f"Warning: Failed to parse get_tasks_tool response for short-circuit: {e}")
        
        elif tool_name == "edit_tasks_tool":
            try:
                parsed = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
                success = (parsed or {}).get("success")
                if success is True:
                    return True
            except Exception as e:
                print(f"Warning: Failed to parse edit_tasks_tool response for short-circuit: {e}")
        
        elif tool_name == "delete_tasks_tool":
            try:
                parsed = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
                success = (parsed or {}).get("success")
                if success is True:
                    return True
            except Exception as e:
                print(f"Warning: Failed to parse delete_tasks_tool response for short-circuit: {e}")
        
        elif tool_name == "create_tasks_tool":
            try:
                parsed = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
                status = (parsed or {}).get("status")
                success = (parsed or {}).get("success")
                if success is True or status in {"all_tasks_created", "invalid_time"}:
                    return True
            except Exception as e:
                print(f"Warning: Failed to parse create_tasks_tool response for short-circuit: {e}")
        
        return False


    def think(self, user_input, scratchpad, user_config=None):
        print(f"🤔 Thinking about user input: {user_input}")
        print(f"📋 Scratchpad provided: {scratchpad is not None}, length: {len(scratchpad) if scratchpad else 0}")
        
        # Check if this exact input was already processed (to prevent infinite loops)
        duplicate_message = check_if_already_processed(scratchpad, user_input)
        if duplicate_message:
            return duplicate_message
        
        # Convert scratchpad entries to chat history format if scratchpad is provided
        chat_history = build_chat_history_from_scratchpad(scratchpad, user_input)
        
        # Add the current user input
        chat_history.append({"role": "user", "content": user_input})
        
        # Debug: Print chat history to see what the agent is seeing
        print(f"📜 Chat history for tool selection: {chat_history}")

        select_tool_agent = SelectToolAgent(self.tool_agents)
        
        # Tool call limits
        MAX_TOTAL_TOOL_CALLS = 10
        MAX_CONSECUTIVE_SAME_TOOL = 3
        total_tool_calls = 0
        previous_tool_name = None
        consecutive_same_tool_count = 0
        
        # Main tool execution loop - unified flow for single and multiple tool calls
        selected_tool_name = None
        selected_tool = None
        
        while selected_tool_name != "generate_response_tool":
            # Get the tool selection response
            selected_tool_response = select_tool_agent.select_tool(chat_history)
            context_msg = " (loop)" if total_tool_calls > 0 else ""
            print(f"Selected tool response{context_msg}: {selected_tool_response}")
            
            # Extract tool names (handles single or multiple uniformly)
            tool_names = self._extract_tool_names_from_response(
                selected_tool_response, 
                context="loop" if total_tool_calls > 0 else "initial"
            )
            
            # Validate tool names
            for tool_name in tool_names:
                if tool_name not in self.tool_agents:
                    if "select_tool" in tool_name.lower():
                        raise ValueError(f"Invalid tool name '{tool_name}'. The model incorrectly returned the selector function name. Available tools are: {list(self.tool_agents.keys())}. Please check the select_tool_agent prompt.")
                    raise KeyError(f"Tool '{tool_name}' not found in available tools: {list(self.tool_agents.keys())}")
            
            # Execute all selected tools sequentially
            # Track if we should short-circuit, but only apply it after ALL tools are processed
            should_short_circuit = False
            
            for tool_name in tool_names:
                # Check if we should stop
                if tool_name == "generate_response_tool":
                    selected_tool_name = tool_name
                    selected_tool = self.tool_agents[selected_tool_name]
                    break
                
                # Check total tool call limit
                if total_tool_calls >= MAX_TOTAL_TOOL_CALLS:
                    print(f"⚠️ Maximum total tool calls ({MAX_TOTAL_TOOL_CALLS}) reached. Forcing generate_response_tool.")
                    selected_tool_name = "generate_response_tool"
                    selected_tool = self.tool_agents[selected_tool_name]
                    break
                
                # Check consecutive same tool limit
                if tool_name == previous_tool_name:
                    consecutive_same_tool_count += 1
                    if consecutive_same_tool_count >= MAX_CONSECUTIVE_SAME_TOOL:
                        print(f"⚠️ Maximum consecutive calls ({MAX_CONSECUTIVE_SAME_TOOL}) for tool '{tool_name}' reached. Forcing generate_response_tool.")
                        selected_tool_name = "generate_response_tool"
                        selected_tool = self.tool_agents[selected_tool_name]
                        break
                else:
                    consecutive_same_tool_count = 1
                    previous_tool_name = tool_name
                
                # Execute the tool
                selected_tool = self.tool_agents[tool_name]
                tool_response = selected_tool.execute_tool(chat_history, user_config)
                print(f"Tool response: {tool_response}")
                total_tool_calls += 1
                
                chat_history.append({"role": "assistant", "name": selected_tool.get_tool_name(), "content": tool_response})
                print(f"Chat history: {chat_history}")
                
                # Check for short-circuit, but don't break yet - process all tool calls first
                # Only short-circuit if this is the last tool call OR if we have a single tool call
                if len(tool_names) == 1 or tool_name == tool_names[-1]:
                    if self._should_short_circuit_to_generate_response(tool_name, tool_response):
                        should_short_circuit = True
            
            # Apply short-circuit after all tool calls are processed
            if should_short_circuit:
                selected_tool_name = "generate_response_tool"
                selected_tool = self.tool_agents[selected_tool_name]
            
            # Break if we're generating response
            if selected_tool_name == "generate_response_tool":
                break

        # Final tool call will be generate response
        response = selected_tool.execute_tool(chat_history, user_config)
        # generate_response_tool returns a string directly, other tools return ChatCompletion objects
        if isinstance(response, str):
            result = response
        elif response is None:
            raise ValueError(f"Tool '{selected_tool_name}' returned None - execution may have failed")
        elif hasattr(response, 'choices') and response.choices:
            result = response.choices[0].message.content
        else:
            raise ValueError(f"Invalid response from tool '{selected_tool_name}': {response}")
        
        # Return both the result and the chat_history (which contains tool responses)
        # This allows tools like edit_tasks_tool to find task_ids from previous tool calls
        return {
            "result": result,
            "chat_history": chat_history
        }