"""Utility functions for processing scratchpad entries and converting them to chat history."""

from .text_utils import normalize_text, should_skip_fragmented_entry


def check_if_already_processed(scratchpad, user_input):
    """Check if this exact input was already processed (to prevent infinite loops).
    
    Checks ALL instances of this user input in the scratchpad, not just the most recent.
    If any instance has a response after it, we should skip processing.
    
    Args:
        scratchpad: List of scratchpad entries
        user_input: The current user input to check
        
    Returns:
        str or None: Returns a skip message if duplicate is found, None otherwise
    """
    if not scratchpad:
        return None
    
    # Normalize current user input for duplicate detection
    normalized_current = normalize_text(user_input)
    
    # Check ALL instances of this user input in the scratchpad, not just the most recent
    # If any instance has a response after it, we should skip processing
    for i, entry in enumerate(scratchpad):
        if entry.get("format") in ["text", "audio"] and entry.get("source") == "user" and entry.get("content"):
            entry_content = entry["content"]
            normalized_entry = normalize_text(entry_content)
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
    
    return None


def build_chat_history_from_scratchpad(scratchpad, user_input):
    """Convert scratchpad entries to chat history format.
    
    Processes scratchpad entries and converts them to a chat history format suitable
    for the agent. Skips fragmented/incomplete audio transcriptions and includes function
    call responses so the agent knows what actions were already taken.
    
    Args:
        scratchpad: List of scratchpad entries
        user_input: The current user input (used for filtering fragmented entries)
        
    Returns:
        list: Chat history in the format expected by the agent
    """
    chat_history = []
    
    if not scratchpad:
        return chat_history
    
    for entry in scratchpad:
        if entry.get("format") in ["text", "audio"]:
            # User inputs
            if entry.get("source") == "user" and entry.get("content"):
                entry_content = entry["content"]
                
                # Skip fragmented/incomplete audio transcriptions
                if should_skip_fragmented_entry(entry_content, user_input):
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
    
    return chat_history
