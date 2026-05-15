"""Gemini tool schemas the developer pipeline can dispatch."""

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
            "Call this only when the user explicitly asks to connect, hand off, or talk to "
            "the remote/other server/operator."
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
