"""Utility functions for extracting task information from chat history."""
import json
import re


def extract_tasks_from_content(content_str):
    """
    Recursively extract tasks from content, handling nested JSON structures.
    
    This function searches for task data in various formats:
    - Direct JSON objects with "tasks" arrays (from get_tasks_tool)
    - JSON objects with "task_id" (from create_tasks_tool, edit_tasks_tool)
    - Nested chat_history arrays containing tool results
    - JSON embedded in text strings
    
    Args:
        content_str: String content that may contain task data in JSON format
        
    Returns:
        List of task dictionaries with keys: task_id, task_info, status, time_to_execute
    """
    tasks_found = []
    if not content_str or not isinstance(content_str, str):
        return tasks_found
    
    # Try to find and parse JSON structures that might contain task data
    # Look for patterns like {"tasks": [...]} or chat_history arrays
    try:
        # Strategy 1: Try to parse the entire content as JSON
        try:
            parsed = json.loads(content_str)
            if isinstance(parsed, dict):
                # Check if it's a get_tasks_tool response
                if parsed.get("tasks"):
                    for task in parsed.get("tasks", []):
                        if task.get("task_id"):
                            tasks_found.append({
                                "task_id": task.get("task_id"),
                                "task_info": task.get("task_info", {}),
                                "status": task.get("status", "pending"),
                                "time_to_execute": task.get("time_to_execute")
                            })
                # Check if it's a create_tasks_tool or edit_tasks_tool response
                if parsed.get("success") and parsed.get("task_id"):
                    tasks_found.append({
                        "task_id": parsed.get("task_id"),
                        "task_info": parsed.get("task_info", {}),
                        "status": parsed.get("status", "pending"),
                        "time_to_execute": parsed.get("time_to_execute")
                    })
                # Check if it contains a chat_history array - recursively process all messages
                if parsed.get("chat_history"):
                    for nested_msg in parsed.get("chat_history", []):
                        # Process get_tasks_tool messages
                        if nested_msg.get("name") == "get_tasks_tool" and nested_msg.get("content"):
                            nested_tasks = extract_tasks_from_content(nested_msg.get("content"))
                            tasks_found.extend(nested_tasks)
                        # Process create_tasks_tool messages
                        if nested_msg.get("name") == "create_tasks_tool" and nested_msg.get("content"):
                            nested_tasks = extract_tasks_from_content(nested_msg.get("content"))
                            tasks_found.extend(nested_tasks)
                        # Process edit_tasks_tool messages
                        if nested_msg.get("name") == "edit_tasks_tool" and nested_msg.get("content"):
                            nested_tasks = extract_tasks_from_content(nested_msg.get("content"))
                            tasks_found.extend(nested_tasks)
                        # Also recursively process any content in nested messages
                        if nested_msg.get("content"):
                            nested_tasks = extract_tasks_from_content(nested_msg.get("content"))
                            tasks_found.extend(nested_tasks)
            elif isinstance(parsed, list):
                # If it's a list, check each item
                for item in parsed:
                    if isinstance(item, dict):
                        nested_tasks = extract_tasks_from_content(json.dumps(item))
                        tasks_found.extend(nested_tasks)
        except json.JSONDecodeError:
            pass
        
        # Strategy 2: Look for get_tasks_tool JSON responses embedded in text
        # Search for patterns like '{"tasks": [...]}'
        # Find all JSON objects that might contain tasks
        json_pattern = r'\{"tasks"\s*:\s*\[.*?\]'
        matches = re.finditer(json_pattern, content_str, re.DOTALL)
        for match in matches:
            try:
                # Try to find the complete JSON object
                start = match.start()
                # Find the matching closing brace
                brace_count = 0
                end = start
                for i in range(start, len(content_str)):
                    if content_str[i] == '{':
                        brace_count += 1
                    elif content_str[i] == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end = i + 1
                            break
                if end > start:
                    json_str = content_str[start:end]
                    parsed = json.loads(json_str)
                    if parsed.get("tasks"):
                        for task in parsed.get("tasks", []):
                            if task.get("task_id"):
                                tasks_found.append({
                                    "task_id": task.get("task_id"),
                                    "task_info": task.get("task_info", {}),
                                    "status": task.get("status", "pending"),
                                    "time_to_execute": task.get("time_to_execute")
                                })
            except (json.JSONDecodeError, ValueError):
                continue
    except Exception:
        pass
    
    return tasks_found


def extract_tasks_from_chat_history(chat_history):
    """
    Extract all available tasks from chat history, including nested structures.
    
    This function processes chat history messages to find tasks from:
    - Direct get_tasks_tool, create_tasks_tool, and edit_tasks_tool messages
    - Nested chat_history structures (e.g., in [Completed in previous interaction] messages)
    - Individual task objects embedded in message content
    
    Args:
        chat_history: List of message dictionaries from the chat history
        
    Returns:
        List of task dictionaries with keys: task_id, task_info, status, time_to_execute
        Tasks are deduplicated by task_id, keeping the most recent entry.
    """
    available_tasks = []
    
    for msg in chat_history:
        # Check for get_tasks_tool results
        if msg.get("name") == "get_tasks_tool" and msg.get("content"):
            try:
                content = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if content.get("tasks"):
                    for task in content.get("tasks", []):
                        if task.get("task_id"):
                            available_tasks.append({
                                "task_id": task.get("task_id"),
                                "task_info": task.get("task_info", {}),
                                "status": task.get("status", "pending"),
                                "time_to_execute": task.get("time_to_execute")
                            })
            except:
                pass
        
        # Check for create_tasks_tool results
        if msg.get("name") == "create_tasks_tool" and msg.get("content"):
            try:
                content = msg["content"]
                # If content is a string, try to parse it as JSON
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        # If JSON parsing fails, try regex extraction as fallback
                        import re
                        task_id_match = re.search(r'"task_id"\s*:\s*"([a-f0-9\-]+)"', content)
                        if task_id_match:
                            task_id = task_id_match.group(1)
                            # Extract task_info if present
                            task_info = {}
                            task_info_match = re.search(r'"task_info"\s*:\s*(\{[^}]+\})', content)
                            if task_info_match:
                                try:
                                    task_info = json.loads(task_info_match.group(1))
                                except:
                                    pass
                            
                            # Extract status
                            status = "pending"
                            status_match = re.search(r'"status"\s*:\s*"([^"]+)"', content)
                            if status_match:
                                status = status_match.group(1)
                            
                            # Extract time_to_execute
                            time_to_execute = None
                            time_match = re.search(r'"time_to_execute"\s*:\s*"([^"]+)"', content)
                            if time_match:
                                time_to_execute = time_match.group(1)
                            
                            available_tasks.append({
                                "task_id": task_id,
                                "task_info": task_info,
                                "status": status,
                                "time_to_execute": time_to_execute
                            })
                            continue  # Successfully extracted via regex, skip JSON parsing
                        else:
                            # No task_id found in string, skip this message
                            raise ValueError("Could not find task_id in create_tasks_tool content")
                
                # If content is now a dict (either was originally or parsed from JSON), extract task info
                if isinstance(content, dict):
                    if content.get("success") and content.get("task_id"):
                        available_tasks.append({
                            "task_id": content.get("task_id"),
                            "task_info": content.get("task_info", {}),
                            "status": content.get("status", "pending"),
                            "time_to_execute": content.get("time_to_execute")
                        })
            except Exception as e:
                # Log the error for debugging but continue processing
                print(f"Warning: Failed to extract task from create_tasks_tool message: {e}")
                print(f"Message content type: {type(msg.get('content'))}, preview: {str(msg.get('content'))[:200] if msg.get('content') else 'None'}")
                pass
        
        # Check for edit_tasks_tool results (these contain the most up-to-date task state)
        if msg.get("name") == "edit_tasks_tool" and msg.get("content"):
            try:
                content = json.loads(msg["content"]) if isinstance(msg["content"], str) else msg["content"]
                if content.get("success") and content.get("task_id"):
                    available_tasks.append({
                        "task_id": content.get("task_id"),
                        "task_info": content.get("task_info", {}),
                        "status": content.get("status", "pending"),
                        "time_to_execute": content.get("time_to_execute")
                    })
            except:
                pass
        
        # Also check message content for nested task data (e.g., in [Completed in previous interaction] messages)
        # This should run for ALL messages to catch nested structures and embedded task data
        if msg.get("content"):
            nested_tasks = extract_tasks_from_content(msg.get("content"))
            available_tasks.extend(nested_tasks)
            
            # Also check for individual task objects embedded in content (fallback for simple cases)
            try:
                content = msg["content"]
                # Try to find JSON task data in the message content
                # The JSON may be appended to text, so try to find it at the end
                
                # Strategy 1: Find the last occurrence of { that might contain task_id
                brace_start = content.rfind('{')
                if brace_start != -1:
                    # Try to extract JSON from this position to the end
                    remaining = content[brace_start:]
                    # Try to parse as JSON
                    try:
                        task_data = json.loads(remaining)
                        if task_data.get("task_id") and not task_data.get("tasks"):
                            # This is an individual task object, not a tasks array
                            available_tasks.append({
                                "task_id": task_data.get("task_id"),
                                "task_info": task_data.get("task_info", {}),
                                "status": task_data.get("status", "pending"),
                                "time_to_execute": task_data.get("time_to_execute")
                            })
                    except json.JSONDecodeError:
                        pass
                
                # Strategy 2: If parsing from last { failed, try to find JSON object with proper brace matching
                # Look for "task_id" and then find the enclosing JSON object
                task_id_pos = content.find('"task_id"')
                if task_id_pos != -1:
                    # Find the opening brace before "task_id"
                    brace_start = content.rfind('{', 0, task_id_pos)
                    if brace_start != -1:
                        # Find the matching closing brace by counting braces
                        brace_count = 0
                        brace_end = -1
                        for i in range(brace_start, len(content)):
                            if content[i] == '{':
                                brace_count += 1
                            elif content[i] == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    brace_end = i + 1
                                    break
                        
                        if brace_end > brace_start:
                            try:
                                json_str = content[brace_start:brace_end]
                                task_data = json.loads(json_str)
                                if task_data.get("task_id") and not task_data.get("tasks"):
                                    # This is an individual task object, not a tasks array
                                    available_tasks.append({
                                        "task_id": task_data.get("task_id"),
                                        "task_info": task_data.get("task_info", {}),
                                        "status": task_data.get("status", "pending"),
                                        "time_to_execute": task_data.get("time_to_execute")
                                    })
                            except (json.JSONDecodeError, ValueError):
                                pass
            except Exception as e:
                # Silently continue if extraction fails
                pass
    
    # Deduplicate available_tasks by task_id, keeping only the most recent entry
    # (later entries in chat_history are more recent)
    task_id_to_task = {}
    for task in available_tasks:
        task_id = task.get("task_id")
        if task_id:
            # If we've seen this task_id before, replace it (keep the most recent)
            task_id_to_task[task_id] = task
    
    # Convert back to list
    return list(task_id_to_task.values())
