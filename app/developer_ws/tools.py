"""OpenAI-shaped tool schemas exposed to Gemini.

`ALL_TOOLS` is passed verbatim to `gemini_reply(...)`. When Gemini chooses a tool,
the pipeline dispatches by `function.name`, matching one of the constants defined
here (e.g. `START_REMOTE_AUDIO_BRIDGE`).
"""

from __future__ import annotations

START_REMOTE_AUDIO_BRIDGE = "start_remote_audio_bridge"

START_REMOTE_AUDIO_BRIDGE_TOOL = {
    "type": "function",
    "function": {
        "name": START_REMOTE_AUDIO_BRIDGE,
        "description": (
            "Route the user to one of their registered agents over a direct audio relay. "
            "Once this returns successfully, the user's microphone audio is forwarded to "
            "the chosen agent and the agent's audio responses are played back to the user "
            "without going through this assistant. Pick the agent_id from the registered "
            "agents list in the system prompt that best matches the user's intent. If the "
            "user names an agent that isn't in the list, do NOT call this tool — tell them "
            "which agents are available instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "ID of the target agent. MUST be one of the agent_id values from "
                        "the registered agents list in the system prompt."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason the user wants to talk to this agent.",
                },
            },
            "required": ["agent_id", "reason"],
        },
    },
}


ALL_TOOLS = [START_REMOTE_AUDIO_BRIDGE_TOOL]
