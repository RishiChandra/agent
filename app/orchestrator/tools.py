"""Tool declarations exposed to Gemini (ORCHESTRATOR_V2_TOOL_CALLS.md §1.1).

Five tools:

  * ``route_to_agent``: M1–M4. Gemini reports what the user's words imply
    (`mode_hint`); the orchestrator's code decides connect vs dispatch and runs
    every call through the validation gate.
  * ``find_agents``: discovery ("is there an agent for…?").
  * ``manage_task``: M5 (status, update, cancel, complete, delete, answer) plus
    confirming or declining a read-back or offer.
  * ``end_conversation``: unchanged.
  * ``google_search``: Gemini's built-in grounding (no Python handler).

Function tools need a handler in ``SpeechPipeline._register_tools``.
"""

from __future__ import annotations

ROUTE_TO_AGENT = "route_to_agent"
FIND_AGENTS = "find_agents"
MANAGE_TASK = "manage_task"
END_CONVERSATION = "end_conversation"

# Gemini function schemas need non-empty object properties, so free-form
# details are passed as a list of name/value pairs (see `pairs_to_dict`).
_NAME_VALUE = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
    "required": ["name", "value"],
}


def pairs_to_dict(value) -> dict:
    """[{name, value}, ...] (or an already-flat dict) -> {name: value}."""
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    out = {}
    for item in value or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out[str(item["name"]).strip()] = item.get("value")
    return out

ROUTE_TO_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": ROUTE_TO_AGENT,
        "description": (
            "Send the user to an agent. Use it when the user wants to talk to an agent live, "
            "OR wants an agent to get something done and report back, OR asks something an "
            "agent owns (their own tasks, reminders, nutrition log, bookings …). The "
            "orchestrator resolves the agent, decides live call vs background task, checks "
            "the details, and speaks to the user itself. Never use it for general-knowledge "
            "questions you can answer, and never invent values the user didn't say."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": (
                        "The agent's name exactly as the user said it (garbles are fine, e.g. "
                        "'cairo's'). Omit if the user didn't name an agent."
                    ),
                },
                "intent": {
                    "type": "string",
                    "description": (
                        "What the user wants, in plain words, as an imperative "
                        "(e.g. 'book a table at Nopa for 2 at 7pm tonight', "
                        "'tell me how many calories I've had today')."
                    ),
                },
                "mode_hint": {
                    "type": "string",
                    "enum": ["connect", "dispatch", "auto"],
                    "description": (
                        "'connect' if the user wants to talk to the agent live ('talk to', "
                        "'put me through'); 'dispatch' if they want it done and reported back "
                        "('have X do', 'let me know when'); otherwise 'auto'."
                    ),
                },
                "slots": {
                    "type": "array",
                    "description": (
                        "Structured details the user actually said, as name/value pairs (e.g. "
                        "restaurant=Nopa, time=19:00, party_size=2). Leave out anything they "
                        "didn't say."
                    ),
                    "items": _NAME_VALUE,
                },
                "notify": {
                    "type": "string",
                    "enum": ["device", "next_session", "silent"],
                    "description": (
                        "'device' only if the user asked to be told when it's done ('let me "
                        "know'); 'silent' if they said not to tell them; otherwise omit."
                    ),
                },
                "deadline": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime, only if the user stated a time limit ('by six', "
                        "'within an hour'). Otherwise omit."
                    ),
                },
                "drop_at_deadline": {
                    "type": "boolean",
                    "description": (
                        "True only if the user said to drop the task if it isn't done by the "
                        "deadline ('forget it if it's not done by six')."
                    ),
                },
            },
            "required": ["intent", "mode_hint"],
        },
    },
}

FIND_AGENTS_TOOL = {
    "type": "function",
    "function": {
        "name": FIND_AGENTS,
        "description": (
            "Look up which registered agents can help with something, when the user asks "
            "'is there an agent for …?' or you are unsure an agent exists. Speaks up to 3 "
            "matches."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What the user is looking for."}},
            "required": ["query"],
        },
    },
}

MANAGE_TASK_TOOL = {
    "type": "function",
    "function": {
        "name": MANAGE_TASK,
        "description": (
            "Manage the user's background tasks, or answer a yes/no question the orchestrator "
            "just asked. Actions: 'status' (is it done?), 'update' (change details), 'cancel', "
            "'complete' (the user says it's done), 'delete', 'answer' (reply to an agent's "
            "question), 'confirm' / 'decline' (the user said yes / no to 'Shall I go ahead?', "
            "'Want me to …?' or 'Want me to put you through?'). The orchestrator reads task "
            "status from its records and speaks the reply itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "update", "cancel", "complete", "delete", "answer", "confirm", "decline"],
                },
                "task_ref": {
                    "type": "string",
                    "description": (
                        "Which task, in the user's words ('that', 'the dinner booking', "
                        "'the Tabletop one'). Omit for the most recent one."
                    ),
                },
                "changes": {
                    "type": "array",
                    "description": "For 'update': only the details the user changed, e.g. time=20:00.",
                    "items": _NAME_VALUE,
                },
                "answer": {"type": "string", "description": "For 'answer': the user's reply, verbatim."},
                "notify": {
                    "type": "string",
                    "enum": ["device", "next_session", "silent"],
                    "description": "For 'update': a change to how the user wants to hear the result.",
                },
            },
            "required": ["action"],
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

# Server-handled (no Python handler; Gemini executes the search itself).
GOOGLE_SEARCH = "google_search"
GOOGLE_SEARCH_TOOL = {"type": GOOGLE_SEARCH}

ALL_TOOLS = [
    ROUTE_TO_AGENT_TOOL,
    FIND_AGENTS_TOOL,
    MANAGE_TASK_TOOL,
    END_CONVERSATION_TOOL,
    GOOGLE_SEARCH_TOOL,
]
