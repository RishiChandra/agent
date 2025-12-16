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
            {"role": "system", "content": f"Given the chat history {chat_history}, select the most appropriate tool to use from the available tools below. You MUST return one of the tool names exactly as listed: {tool_names_list}. {tool_descriptions}"},
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
