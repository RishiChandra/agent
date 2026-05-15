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
            "Open a direct audio relay to a remote server. Once this returns successfully, "
            "the user's microphone audio is forwarded to that remote and the remote's audio "
            "responses are played back to the user without going through this assistant. "
            "Call this when the user says any of: 'call the service', 'call the server', "
            "'call the remote', 'connect to the service/remote/operator', 'dial the service', "
            "'hand off to the remote/operator', or anything clearly equivalent in intent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Short reason the user wants the bridge opened.",
                },
            },
            "required": ["reason"],
        },
    },
}


ALL_TOOLS = [START_REMOTE_AUDIO_BRIDGE_TOOL]
