"""Tool declarations exposed to Gemini.

Two shapes are supported in `ALL_TOOLS`:

  * ``{"type": "function", "function": {...}}`` — standard function-call tool.
    Gemini emits a function-call frame; ``SpeechPipeline._register_tools`` has a
    matching Python handler in our process that runs the side effect.

  * ``{"type": "google_search"}`` — Gemini's built-in Google Search grounding.
    No Python handler needed; Gemini executes the search server-side and
    incorporates the results into its text response (with citations in the
    grounding metadata). Declared here so the model knows it can use search
    for current-events / outside-training-data queries.

``ALL_TOOLS`` is forwarded to ``CustomGeminiLLMService.set_tools_schema(...)``
in one place, alongside handler registration, so the schemas Gemini sees and
the handlers we dispatch to stay in lock-step.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Function-call tools (need a handler in SpeechPipeline._register_tools)
# ---------------------------------------------------------------------------

START_REMOTE_AUDIO_BRIDGE = "start_remote_audio_bridge"
END_CONVERSATION = "end_conversation"

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

END_CONVERSATION_TOOL = {
    "type": "function",
    "function": {
        "name": END_CONVERSATION,
        "description": (
            "Close the conversation and disconnect the session. Call this when the user "
            "indicates they are done talking — for example saying 'goodbye', 'bye', "
            "'thanks, that's all', 'thanks, that's enough', 'I'm done', 'see you later', "
            "'no, I'm good', 'we're done here', or anything clearly equivalent in intent. "
            "Use judgment: do NOT call this if the user is asking a follow-up question, "
            "thanking you mid-conversation, or still actively engaged. Only call it when "
            "the user is genuinely wrapping up. After this is called the assistant speaks "
            "a brief goodbye and the WebSocket closes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Short reason the conversation is ending — what the user said or "
                        "indicated (e.g. 'user said goodbye', 'user thanked and signed off')."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
}


# ---------------------------------------------------------------------------
# Server-handled tools (no Python handler; Gemini executes server-side)
# ---------------------------------------------------------------------------

# Sentinel string used by `SpeechPipeline._register_tools` to know which tools
# require a Python handler vs which are server-side. Match by `tool["type"]`.
GOOGLE_SEARCH = "google_search"

GOOGLE_SEARCH_TOOL = {
    "type": GOOGLE_SEARCH,
    # No "function" key — the Gemini tool-converter recognises this `type`
    # and emits `types.Tool(google_search=types.GoogleSearch())`. Gemini
    # decides on its own when to ground a response in Google Search; we do
    # not see a function-call frame for this tool. Used for:
    #   - current events ("what's the score of yesterday's game")
    #   - facts that change ("who is the current CEO of X")
    #   - anything plausibly outside the model's training cutoff.
}


ALL_TOOLS = [
    START_REMOTE_AUDIO_BRIDGE_TOOL,
    END_CONVERSATION_TOOL,
    GOOGLE_SEARCH_TOOL,
]
