from .openai_client import call_openai

class SelectToolAgent:
    tool_agents = {}

    def __init__(self, tool_agents):
        self.tool_agents = tool_agents

    def select_tool(self, chat_history):
        # Create a system message with tool descriptions
        tool_descriptions = "Available tools:\n"
        for tool_agent in self.tool_agents.values():
            print(f"Tool agent: {tool_agent}")
            tool_descriptions += f"{tool_agent.get_tool_name()}: {tool_agent.get_tool_description()}\n"

        # Create messages array for OpenAI API
        tool_name_enum = [tool_agent.get_tool_name() for tool_agent in self.tool_agents.values()]
        tool_names_list = ", ".join(tool_name_enum)
        
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a tool selector. "
                    f"Given the chat history {chat_history}, select the most appropriate tool(s) "
                    "to call from the list below.\n\n"
                    "## How to Determine if Work is Already Complete\n"
                    "- Assistant acknowledgments (e.g., 'One moment', 'Let me check', 'I'll set that up') are NOT completions.\n"
                    "- Work is only complete if you see:\n"
                    "  * A '[Completed in previous interaction via TOOL_NAME]' message, OR\n"
                    "  * An assistant message with name='create_tasks_tool' containing JSON with 'success': true and 'task_id', OR\n"
                    "  * An explicit success message from a tool execution\n\n"
                    "## CRITICAL: Only Look at the MOST RECENT User Message\n"
                    "- When determining how many tasks to create, ONLY look at the MOST RECENT user message in the chat history\n"
                    "- IGNORE tasks mentioned in previous conversations or earlier messages\n"
                    "- ONLY create tasks that are explicitly mentioned in the CURRENT user request\n"
                    "- If the most recent user message says 'remind me to eat dinner at 10pm', that's ONE task - do NOT create tasks from previous messages\n\n"
                    "## Tool Selection Rules\n"
                    "0. **CRITICAL: For READ/VIEW/CHECK/WHAT/WHEN queries**:\n"
                    "   - If the user asks 'What tasks do I have', 'Show me my tasks', 'When do I have X', 'Check my tasks', etc. → select 'get_tasks_tool' ONLY if you don't see a 'get_tasks_tool' response in the chat history yet\n"
                    "   - CRITICAL: If you see ANY assistant message with name='get_tasks_tool' in the chat history (even if it returns empty results like '{\"tasks\": [], \"total_count\": 0}'), you MUST select 'generate_response_tool' immediately\n"
                    "   - Empty task results ({\"tasks\": [], \"total_count\": 0}) are VALID results - do NOT call 'get_tasks_tool' again\n"
                    "   - NEVER select 'create_tasks_tool' for read-only queries, even if you see task data in the chat history\n"
                    "   - Read-only queries should NEVER trigger task creation\n\n"
                    "1. **For CREATE/SCHEDULE/SET/ADD requests with MULTIPLE tasks**:\n"
                    "   - Look ONLY at the MOST RECENT user message\n"
                    "   - If that message requests multiple tasks (e.g., 'remind me to X at Y and Z at W'), return MULTIPLE 'create_tasks_tool' calls - ONE for each task requested\n"
                    "   - Count how many distinct tasks the user requested in the MOST RECENT message and return that many 'create_tasks_tool' calls\n"
                    "   - Each call will extract a different task from the MOST RECENT user request\n"
                    "   - Example: If the most recent user message says 'remind me to brush teeth at 6am and pack bag at 11am', return 2 'create_tasks_tool' calls\n"
                    "   - DO NOT create tasks from previous user messages - only from the most recent one\n\n"
                    "2. **For CREATE/SCHEDULE/SET/ADD requests with a SINGLE task**:\n"
                    "   - Look ONLY at the MOST RECENT user message\n"
                    "   - If that message requests a single task and no task was created yet for THIS request → select 'create_tasks_tool'\n"
                    "   - CRITICAL: If you see ANY assistant message with name='create_tasks_tool' containing JSON with 'success': true AFTER the most recent user message → select 'generate_response_tool' immediately (the task has already been created)\n"
                    "   - If a task was already created for THIS request → select 'generate_response_tool' (do NOT select 'create_tasks_tool' again)\n"
                    "   - If you see an assistant message with name='create_tasks_tool' whose JSON includes 'success': false → select 'generate_response_tool' (the tool already determined the request cannot be completed as requested; ask the user for a new time)\n"
                    "   - If you see an assistant message with name='create_tasks_tool' whose JSON includes 'status': 'all_tasks_created' → select 'generate_response_tool' (all tasks from the most recent user message have been created)\n"
                    "   - Do NOT use 'generate_response_tool' for new task creation requests\n\n"
                    "3. **After tasks have been created**:\n"
                    "   - Find the MOST RECENT user message in the chat history\n"
                    "   - Count how many distinct tasks the user requested in that MOST RECENT user message ONLY\n"
                    "   - Count how many 'create_tasks_tool' responses with 'success': true appear AFTER that most recent user message\n"
                    "   - If the counts match (all tasks from the most recent message created) → select 'generate_response_tool'\n"
                    "   - If the counts don't match (some tasks from the most recent message still need to be created) → select 'create_tasks_tool' again\n"
                    "   - IMPORTANT: Only select 'generate_response_tool' when you are CERTAIN all tasks from the MOST RECENT user message have been created\n"
                    "   - If you see 'status': 'all_tasks_created' in a create_tasks_tool response → select 'generate_response_tool' immediately\n\n"
                    "4. **For EDIT/UPDATE/COMPLETE/DEFER requests**:\n"
                    "   - CRITICAL: If the user says they have completed a task (e.g., 'I completed X', 'I finished Y', 'I did Z', 'I'm done with X') → select 'edit_tasks_tool' to mark the task as completed\n"
                    "   - CRITICAL: If the user wants to defer the task (e.g., 'I'll do it later', 'not yet', 'I need more time', 'I haven't finished X', 'I'm not done with Y', 'I'm not done yet', 'remind me later', 'not finished') → select 'edit_tasks_tool' to defer the task by 5 minutes (update time_to_execute)\n"
                    "   - If the user asks to 'mark as complete', 'complete', 'mark as done', 'mark as pending', 'uncomplete', 'reopen', etc. → select 'edit_tasks_tool'\n"
                    "   - CRITICAL: 'edit_tasks_tool' should ONLY be called when the agent has a SPECIFIC task_id from chat history / previous tool calls\n"
                    "   - The agent must have a task_id from previous 'get_tasks_tool' or 'create_tasks_tool' calls in the chat history\n"
                    "   - If no task_id is available in chat history, first use 'get_tasks_tool' to retrieve tasks (which will provide task_ids), then use 'edit_tasks_tool'\n"
                    "   - Do NOT call 'edit_tasks_tool' if you cannot find a specific task_id in the chat history\n"
                    "   - After 'edit_tasks_tool' completes successfully → select 'generate_response_tool'\n"
                    "   - If 'edit_tasks_tool' returns 'success': false → select 'generate_response_tool' to inform the user of the error\n\n"
                    "5. **For DELETE/REMOVE/CANCEL requests**:\n"
                    "   - CRITICAL: If the user explicitly asks to DELETE, REMOVE, or CANCEL a task (e.g., 'delete X', 'remove Y', 'cancel Z', 'delete that task', 'remove my reminder for X') → select 'delete_tasks_tool'\n"
                    "   - CRITICAL: 'delete_tasks_tool' should ONLY be called when the agent has a SPECIFIC task_id from chat history / previous tool calls\n"
                    "   - The agent must have a task_id from previous 'get_tasks_tool' or 'create_tasks_tool' calls in the chat history\n"
                    "   - If no task_id is available in chat history, first use 'get_tasks_tool' to retrieve tasks (which will provide task_ids), then use 'delete_tasks_tool'\n"
                    "   - Do NOT call 'delete_tasks_tool' if you cannot find a specific task_id in the chat history\n"
                    "   - After 'delete_tasks_tool' completes successfully → select 'generate_response_tool'\n"
                    "   - If 'delete_tasks_tool' returns 'success': false → select 'generate_response_tool' to inform the user of the error\n\n"
                    "6. **For other requests**:\n"
                    "   - Select the appropriate tool for the user's request\n"
                    "   - Do NOT duplicate work already completed in the chat history\n"
                    "   - If the request is already satisfied → select 'generate_response_tool'\n\n"
                    "7. **General principles**:\n"
                    "   - New user requests (even if similar to previous ones) should be handled with the appropriate tool\n"
                    "   - Prefer tools that move the conversation forward\n"
                    "   - Only use 'generate_response_tool' when ALL requested tasks are created AND the request is satisfied\n\n"
                    f"## Available Tools\n"
                    f"You MUST return one or more of these tool names exactly: {tool_names_list}\n"
                    f"{tool_descriptions}"
                ),
            },
        ]

        selecting_tool = {
            "type": "function",
            "function": {
                "name": "select_tool",
                "description": "Selects the most appropriate tool to use from the available tools. Returns the exact name of one of the available tools (NOT 'select_tool' itself).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "enum": tool_name_enum,
                            "description": f"The exact name of the tool to use. Must be one of: {tool_names_list}. Do NOT return 'select_tool' or 'functions.select_tool'.",
                        },
                    },
                    "required": ["tool_name"],
                },
            }
        }
        return call_openai(messages, [selecting_tool])
