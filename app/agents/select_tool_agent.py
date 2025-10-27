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
        messages = [
            {"role": "system", "content": f"Given the chat history {chat_history}, select the most appropriate tool to use. {tool_descriptions}"},
        ]

        tool_name_enum = [tool_agent.get_tool_name() for tool_agent in self.tool_agents.values()]

        selecting_tool = {
            "type": "function",
            "function": {
                "name": "select_tool",
                "description": "Selects the most appropriate tool to use and returns the name of the tool to use.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "enum": tool_name_enum,
                            "description": "The name of the tool to use.",
                        },
                    },
                    "required": ["tool_name"],
                },
            }
        }
        return call_openai(messages, [selecting_tool])
