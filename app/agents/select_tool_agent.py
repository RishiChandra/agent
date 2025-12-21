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
                    f"Given the chat history {chat_history}, select the single most appropriate tool "
                    "to call from the list below.\n\n"
                    "IMPORTANT BEHAVIOR RULES:\n"
                    "- Do NOT duplicate work that has already been completed in the chat history.\n"
                    "- If a user's request appears to have been fully handled by a previous tool call "
                    "(for example, a task was already created, or tasks were already fetched), "
                    "do NOT select that tool again just to repeat the same operation.\n"
                    "- However, if the user is making a NEW request (even if similar to a previous one), "
                    "you should select the appropriate tool for that NEW request.\n"
                    "- If the user is asking to CREATE, SCHEDULE, SET, or ADD a task, reminder, or todo item, "
                    "you MUST select the 'create_tasks_tool' - do NOT use the response-generating tool.\n"
                    "- Prefer tools that move the conversation forward based on what has already happened.\n"
                    "- Only use the response-generating tool if the user's latest request is already satisfied "
                    "by prior tool outputs AND the user is not asking to create a new task.\n\n"
                    f"You MUST return one of the tool names exactly as listed: {tool_names_list}.\n"
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
