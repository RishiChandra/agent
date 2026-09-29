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
DISPATCH_TASK = "dispatch_task"
FIND_AGENTS = "find_agents"
CHECK_TASKS = "check_tasks"
CANCEL_TASK = "cancel_task"
ANSWER_AGENT = "answer_agent"

START_REMOTE_AUDIO_BRIDGE_TOOL = {
    "type": "function",
    "function": {
        "name": START_REMOTE_AUDIO_BRIDGE,
        "description": (
            "Open a direct audio relay to a registered agent (remote server). Once this "
            "returns successfully, the user's microphone audio is forwarded to that agent and "
            "the agent's audio responses are played back to the user without going through "
            "this assistant. Call this when the user says any of: 'call the service', 'call "
            "the server', 'call the remote', 'connect to the service/remote/operator', 'dial "
            "the service', 'hand off to the remote/operator', 'talk to the <name> agent', or "
            "anything clearly equivalent in intent. If the user names a specific agent, pass "
            "its name in the `agent` argument so the call routes to the right one. Use this "
            "for a live, back-and-forth conversation with an agent; if the user just wants "
            "something DONE and reported back, use dispatch_task instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Short reason the user wants the bridge opened.",
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Name (or service id) of the registered agent to connect to, as the "
                        "user referred to it — e.g. 'weather bot'. If it matches an agent "
                        "listed in the system prompt, pass that registered name; otherwise "
                        "pass what the user said verbatim — the orchestrator resolves it "
                        "(including speech-to-text garbles) and asks the user if it's "
                        "ambiguous. Omit if the user did not name a specific agent, in which "
                        "case the default configured agent is used."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
}

DISPATCH_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": DISPATCH_TASK,
        "description": (
            "Hand a task to a registered agent to carry out in the background, and report "
            "back when it's done — e.g. 'have the booking agent reserve a table for two at "
            "7', 'ask Ledger to file my Uber receipt', 'get the travel agent to find flights "
            "to Denver Friday'. The user stays talking to you while the agent works; its "
            "result is announced automatically. Call this ONLY when the request clearly "
            "needs an agent (an action in an external system or specialised knowledge) AND "
            "you have what the task needs. If essential details are missing (e.g. no date "
            "for a booking), ask the user a short follow-up question instead of calling. If "
            "you can answer directly from general knowledge, just answer. Do not call it for "
            "a live conversation with an agent — that's start_remote_audio_bridge."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": (
                        "The task, as one clear imperative sentence the agent can act on, "
                        "including every detail the user gave (who/what/when/where)."
                    ),
                },
                "agent": {
                    "type": "string",
                    "description": (
                        "Agent name as the user said it, if they named one. Omit to let the "
                        "orchestrator pick the best capable agent for the intent."
                    ),
                },
                "details": {
                    "type": "string",
                    "description": "Optional extra structured context (constraints, preferences).",
                },
            },
            "required": ["intent"],
        },
    },
}

FIND_AGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": FIND_AGENTS,
        "description": (
            "Look up which registered agents can help with something. Call this when the "
            "user asks what agents/services exist, whether there's an agent for X, or when "
            "you're unsure an agent exists for their request before dispatching or bridging. "
            "The matches are read out to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the user wants an agent for, or a name they mentioned.",
                },
            },
            "required": ["query"],
        },
    },
}

CHECK_TASKS_TOOL = {
    "type": "function",
    "function": {
        "name": CHECK_TASKS,
        "description": (
            "Report the status of tasks previously handed to agents — call when the user asks "
            "'is it done yet?', 'what's the status of my booking?', 'what are my agents doing?'."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

CANCEL_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": CANCEL_TASK,
        "description": (
            "Cancel a task that an agent is still working on — call when the user says "
            "'cancel that', 'never mind the booking', 'stop the agent'. Defaults to the most "
            "recent unfinished task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Specific task id, if known."},
            },
        },
    },
}

ANSWER_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": ANSWER_AGENT,
        "description": (
            "Pass the user's answer back to an agent that asked a question about a running "
            "task (the assistant said '<agent> needs something from you: ...'). Call this "
            "when the user's reply is answering that question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "The user's answer, verbatim."},
                "task_id": {"type": "string", "description": "Specific task id, if known."},
            },
            "required": ["answer"],
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
            "thanking you mid-conversation, or still actively engaged. "
            "EXCEPTION — interrupted speech: if the user's message begins with "
            "'[interrupted assistant mid-reply]', they cut the assistant off while it was "
            "talking, and phrases like 'stop', 'okay', 'that's enough' mean STOP TALKING, "
            "not end the session — do NOT call this tool then, unless the message also "
            "contains a clear farewell such as 'goodbye' or 'end the call'. "
            "Only call it when the user is genuinely wrapping up. After this is called "
            "the assistant speaks a brief goodbye and the WebSocket closes."
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
    DISPATCH_TASK_TOOL,
    FIND_AGENTS_TOOL,
    CHECK_TASKS_TOOL,
    CANCEL_TASK_TOOL,
    ANSWER_AGENT_TOOL,
    END_CONVERSATION_TOOL,
    GOOGLE_SEARCH_TOOL,
]
