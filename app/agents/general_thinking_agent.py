import logging
import asyncio

from .select_tool_agent import SelectToolAgent
import json


from .openai_client import call_openai
from .tool_agents.get_tasks_tool_agent import GetTasksToolAgent
from .tool_agents.create_tasks_tool_agent import CreateTasksToolAgent
from .tool_agents.edit_tasks_tool_agent import EditTasksToolAgent
from .tool_agents.generate_response_tool_agent import GenerateResponseToolAgent

class GeneralThinkingAgent:
    tool_agents = {}
    def __init__(self):
        agents = [GetTasksToolAgent(), CreateTasksToolAgent(), EditTasksToolAgent(), GenerateResponseToolAgent()]
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

    def _normalize_text(self, text):
        """Normalize text for comparison by lowercasing and removing extra whitespace.
        
        Args:
            text: The text to normalize
            
        Returns:
            str: Normalized text (lowercase, single spaces, trimmed)
        """
        if not text:
            return ""
        return " ".join(text.lower().strip().split())
    
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
        entry_normalized = self._normalize_text(entry_content)
        final_normalized = self._normalize_text(final_user_input)
        
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

    def think(self, user_input, scratchpad, user_config=None):
        print(f"🤔 Thinking about user input: {user_input}")
        print(f"📋 Scratchpad provided: {scratchpad is not None}, length: {len(scratchpad) if scratchpad else 0}")
        
        # Initialize chat history with scratchpad entries
        chat_history = []
        
        # Normalize current user input for duplicate detection
        normalized_current = self._normalize_text(user_input)
        
        # Check if this exact input was already processed (to prevent infinite loops)
        if scratchpad:
            # Check ALL instances of this user input in the scratchpad, not just the most recent
            # If any instance has a response after it, we should skip processing
            for i, entry in enumerate(scratchpad):
                if entry.get("format") in ["text", "audio"] and entry.get("source") == "user" and entry.get("content"):
                    entry_content = entry["content"]
                    normalized_entry = self._normalize_text(entry_content)
                    if normalized_current == normalized_entry:
                        # Check if there's already a completed response after this user input
                        # Look ahead in scratchpad to see if this was already processed
                        for later_entry in scratchpad[i + 1:]:
                            # Check for function_call response (definite completion)
                            if (later_entry.get("format") == "function_call" and 
                                later_entry.get("source") == "agent" and 
                                later_entry.get("response") and 
                                later_entry.get("response").get("result")):
                                print(f"⚠️ Duplicate user input detected (already processed with function_call), skipping: {user_input[:50]}...")
                                return "This request has already been processed. Please check the previous response."
                            # Check for assistant responses that indicate completion (not just acknowledgments)
                            if (later_entry.get("format") in ["text", "audio"] and 
                                later_entry.get("source") == "agent" and 
                                later_entry.get("content")):
                                content = later_entry.get("content", "")
                                # Skip if it's just a brief acknowledgment (like "Let me check", "One moment")
                                acknowledgment_phrases = ["let me check", "one moment", "looking", "checking"]
                                is_acknowledgment = any(phrase in content.lower() for phrase in acknowledgment_phrases) and len(content) < 50
                                # If it's a substantial response (not just an acknowledgment), it was already processed
                                if not is_acknowledgment and len(content) > 20:
                                    print(f"⚠️ Duplicate user input detected (already processed with assistant response), skipping: {user_input[:50]}...")
                                    return "This request has already been processed. Please check the previous response."
                            # If we encounter another user input before finding a response, stop checking this instance
                            if later_entry.get("format") in ["text", "audio"] and later_entry.get("source") == "user":
                                break
        
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
                        tool_name = entry.get("name", "tool")
                        
                        # For think_and_repeat_output, include the actual tool responses if available
                        # This allows tools like edit_tasks_tool to find task_ids from previous tool calls
                        if tool_name == "think_and_repeat_output":
                            # First, include the actual tool responses (create_tasks_tool, get_tasks_tool, etc.)
                            # These are stored in the response's tool_responses field
                            if entry.get("response").get("tool_responses"):
                                for tool_response in entry["response"]["tool_responses"]:
                                    if isinstance(tool_response, dict) and tool_response.get("name") and tool_response.get("content"):
                                        chat_history.append({
                                            "role": "assistant",
                                            "name": tool_response["name"],
                                            "content": tool_response["content"]
                                        })
                            
                            # Then include the formatted result message
                            tool_context = f"[Completed in previous interaction via {tool_name}]: {result_content}"
                            chat_history.append({
                                "role": "assistant",
                                "content": tool_context
                            })
                        else:
                            # For other function calls, just include the result
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
            tool_calls = selected_tool_response.choices[0].message.tool_calls
            # Process all tool calls if multiple are returned
            if len(tool_calls) > 1:
                print(f"⚠️ Multiple tool calls detected ({len(tool_calls)}). Processing all of them.")
                # Process all tool calls of the same type sequentially, updating chat_history after each
                for idx, tool_call in enumerate(tool_calls):
                    tool_name = json.loads(tool_call.function.arguments)["tool_name"]
                    if tool_name not in self.tool_agents:
                        print(f"⚠️ Skipping invalid tool '{tool_name}' in multi-call response")
                        continue
                    tool = self.tool_agents[tool_name]
                    print(f"Processing tool call {idx + 1}/{len(tool_calls)}: {tool_name}")
                    # Use updated chat_history so subsequent calls see previous results
                    tool_response = tool.execute_tool(chat_history, user_config)
                    print(f"Tool response {idx + 1}: {tool_response}")
                    # Immediately update chat_history so next iteration sees this result
                    chat_history.append({"role": "assistant", "name": tool.get_tool_name(), "content": tool_response})
                    total_tool_calls += 1
                # After processing all, get next tool selection
                selected_tool_response = select_tool_agent.select_tool(chat_history)
                print(f"Selected tool response (after multi-call): {selected_tool_response}")
                # Validate and extract next tool name
                if not selected_tool_response.choices or not selected_tool_response.choices[0].message.tool_calls:
                    print(f"Error: No tool_calls in response after multi-call. Response structure: {selected_tool_response}")
                    raise ValueError(f"No tool_calls found in response after processing multiple calls.")
                tool_call = selected_tool_response.choices[0].message.tool_calls[0]
                selected_tool_name = json.loads(tool_call.function.arguments)["tool_name"]
            else:
                tool_call = tool_calls[0]
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
            
            tool_response = selected_tool.execute_tool(chat_history, user_config)
            print(f"Tool response: {tool_response}")
            total_tool_calls += 1

            chat_history.append({"role": "assistant", "name": selected_tool.get_tool_name(), "content": tool_response})
            print(f"Chat history: {chat_history}")

            # Deterministic short-circuit: if we just ran get_tasks_tool, we should NOT call it again.
            # Empty results are valid - we should generate a response.
            if selected_tool_name == "get_tasks_tool":
                try:
                    parsed = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
                    # get_tasks_tool always returns a valid response (even if empty)
                    # If we see a response with "tasks" key, it's valid and we should generate a response
                    if parsed and ("tasks" in parsed or "total_count" in parsed):
                        selected_tool_name = "generate_response_tool"
                        selected_tool = self.tool_agents[selected_tool_name]
                        break
                except Exception as e:
                    print(f"Warning: Failed to parse get_tasks_tool response for short-circuit: {e}")
            
            # Deterministic short-circuit: if we just ran edit_tasks_tool and it succeeded,
            # then we should NOT call edit_tasks_tool again for the same user message.
            if selected_tool_name == "edit_tasks_tool":
                try:
                    parsed = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
                    success = (parsed or {}).get("success")
                    if success is True:
                        selected_tool_name = "generate_response_tool"
                        selected_tool = self.tool_agents[selected_tool_name]
                        break
                except Exception as e:
                    print(f"Warning: Failed to parse edit_tasks_tool response for short-circuit: {e}")
            
            # Deterministic short-circuit: if we just ran create_tasks_tool and it either
            # (a) succeeded, or (b) reported that the time is invalid / all tasks are created,
            # then we should NOT call create_tasks_tool again for the same user message.
            if selected_tool_name == "create_tasks_tool":
                try:
                    parsed = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
                    status = (parsed or {}).get("status")
                    success = (parsed or {}).get("success")
                    if success is True or status in {"all_tasks_created", "invalid_time"}:
                        selected_tool_name = "generate_response_tool"
                        selected_tool = self.tool_agents[selected_tool_name]
                        break
                except Exception as e:
                    print(f"Warning: Failed to parse create_tasks_tool response for short-circuit: {e}")

            # Get next tool selection
            selected_tool_response = select_tool_agent.select_tool(chat_history)
            print(f"Selected tool response (loop): {selected_tool_response}")
            
            # Validate response structure
            if not selected_tool_response.choices or not selected_tool_response.choices[0].message.tool_calls:
                print(f"Error: No tool_calls in response. Response structure: {selected_tool_response}")
                raise ValueError(f"No tool_calls found in response. Message content: {selected_tool_response.choices[0].message.content if selected_tool_response.choices else 'No choices'}")
            
            # Extract tool name from the response
            try:
                tool_calls = selected_tool_response.choices[0].message.tool_calls
                # Process all tool calls if multiple are returned
                if len(tool_calls) > 1:
                    print(f"⚠️ Multiple tool calls detected ({len(tool_calls)}) in loop. Processing all of them.")
                    # Process all tool calls of the same type sequentially, updating chat_history after each
                    for idx, tool_call in enumerate(tool_calls):
                        tool_name = json.loads(tool_call.function.arguments)["tool_name"]
                        if tool_name not in self.tool_agents:
                            print(f"⚠️ Skipping invalid tool '{tool_name}' in multi-call response")
                            continue
                        tool = self.tool_agents[tool_name]
                        print(f"Processing tool call {idx + 1}/{len(tool_calls)}: {tool_name}")
                        # Use updated chat_history so subsequent calls see previous results
                        tool_response = tool.execute_tool(chat_history, user_config)
                        print(f"Tool response {idx + 1}: {tool_response}")
                        # Immediately update chat_history so next iteration sees this result
                        chat_history.append({"role": "assistant", "name": tool.get_tool_name(), "content": tool_response})
                        total_tool_calls += 1
                    # After processing all, get next tool selection
                    selected_tool_response = select_tool_agent.select_tool(chat_history)
                    print(f"Selected tool response (after multi-call loop): {selected_tool_response}")
                    # Validate and extract next tool name
                    if not selected_tool_response.choices or not selected_tool_response.choices[0].message.tool_calls:
                        print(f"Error: No tool_calls in response after multi-call loop. Response structure: {selected_tool_response}")
                        raise ValueError(f"No tool_calls found in response after processing multiple calls in loop.")
                    tool_call = selected_tool_response.choices[0].message.tool_calls[0]
                    selected_tool_name = json.loads(tool_call.function.arguments)["tool_name"]
                else:
                    tool_call = tool_calls[0]
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