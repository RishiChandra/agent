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

    def _has_fragmentation(self, text):
        """Check if text has fragmentation patterns indicating incomplete audio transcription.
        
        Fragmented transcriptions often have:
        - Double spaces
        - Single letter words (except common ones like "a", "i")
        - Spaces within words (like "cre ate" instead of "create")
        - Spaces around punctuation (like "4 :00" or "a .m." instead of "4:00" or "a.m.")
        
        Args:
            text: The text to check for fragmentation
            
        Returns:
            bool: True if fragmentation patterns are detected
        """
        words = text.split()
        # Check for patterns that indicate fragmentation
        has_double_spaces = "  " in text
        has_spaces_in_words = any(" " in word for word in words)
        
        # Check for single letter words (excluding common ones like "a", "i", "I")
        common_single_letters = {"a", "i", "A", "I"}
        has_single_letter_words = any(
            len(word) == 1 and word.isalpha() and word not in common_single_letters 
            for word in words
        )
        
        # Check for spaces around punctuation/numbers (like "4 :00", "a .m.", ":00 a .m.")
        has_spaces_around_punctuation = (
            " :" in text or  # Space before colon
            " ." in text or  # Space before period
            ": " in text and " :" in text  # Both patterns present
        )
        
        return has_double_spaces or has_single_letter_words or has_spaces_in_words or has_spaces_around_punctuation

    def _should_skip_fragmented_entry(self, entry_content, final_user_input):
        """Determine if a scratchpad entry should be skipped because it's a fragmented transcription.
        
        Fragmented audio transcriptions should be skipped when we have a complete final user input
        to avoid creating duplicate or incorrect tasks.
        
        Args:
            entry_content: The content from the scratchpad entry
            final_user_input: The final, complete user input being processed
            
        Returns:
            bool: True if the entry should be skipped
        """
        if not entry_content or not final_user_input:
            return False
        
        # Normalize for comparison (lowercase, remove extra spaces)
        entry_normalized = " ".join(entry_content.lower().split())
        final_normalized = " ".join(final_user_input.lower().split())
        
        if not entry_normalized or not final_normalized:
            return False
        
        # Check for fragmentation patterns
        entry_has_fragmentation = self._has_fragmentation(entry_content)
        final_has_fragmentation = self._has_fragmentation(final_user_input)
        
        # Skip if entry is a fragment that's a substring of the final user input
        if entry_has_fragmentation and entry_normalized in final_normalized:
            return True
        
        # Skip if entry is much shorter (clearly incomplete)
        if entry_has_fragmentation and len(entry_normalized) < len(final_normalized) * 0.7:
            return True
        
        # Skip if entry has fragmentation but final input is complete and they're similar in length
        # This catches cases where the fragment is mis-transcribed (like "ck my ra nge" vs "pack my rain jacket")
        if entry_has_fragmentation and not final_has_fragmentation:
            # If lengths are similar (within 30%), likely the same request with different transcription quality
            length_ratio = len(entry_normalized) / len(final_normalized) if final_normalized else 0
            if 0.7 <= length_ratio <= 1.3:
                # Similar length, but entry is fragmented and final is complete - skip the fragment
                return True
        
        return False

    def think(self, user_input, scratchpad):
        print(f"🤔 Thinking about user input: {user_input}")
        print(f"📋 Scratchpad provided: {scratchpad is not None}, length: {len(scratchpad) if scratchpad else 0}")
        
        # Initialize chat history with scratchpad entries
        chat_history = []
        
        # Convert scratchpad entries to chat history format if scratchpad is provided
        if scratchpad:
            for entry in scratchpad:
                if entry.get("format") in ["text", "audio"]:
                    # User inputs
                    if entry.get("source") == "user" and entry.get("content"):
                        entry_content = entry["content"]
                        
                        # Skip fragmented/incomplete audio transcriptions
                        if self._should_skip_fragmented_entry(entry_content, user_input):
                            continue
                        
                        chat_history.append({
                            "role": "user",
                            "content": entry_content
                        })
                    # Agent responses
                    elif entry.get("source") == "agent" and entry.get("content"):
                        chat_history.append({
                            "role": "assistant",
                            "content": entry["content"]
                        })
                # Include function call responses so the agent knows what actions were already taken
                elif entry.get("format") == "function_call" and entry.get("source") == "agent":
                    # Include function call responses - these contain the result of tool execution
                    # This helps the agent understand what actions have already been completed
                    if entry.get("response") and entry.get("response").get("result"):
                        # The result contains information about what was done (e.g., "Task created successfully")
                        result_content = entry["response"]["result"]
                        # Make it clear this is a completed action from a previous interaction
                        # Note: This is the final response message, not the tool execution result
                        # But it indicates what action was taken in response to a previous user request
                        tool_name = entry.get("name", "tool")
                        # Format to make it clear this represents a completed action from a previous turn
                        tool_context = f"[Completed in previous interaction via {tool_name}]: {result_content}"
                        chat_history.append({
                            "role": "assistant",
                            "content": tool_context
                        })
        
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