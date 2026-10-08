"""Every fixed line the orchestrator speaks (ORCHESTRATOR_V2_TOOL_CALLS.md §1.8).

Handlers speak these verbatim (decision D1): no LLM rephrasing, so they are
exact and cost no extra Gemini call. Keep the doc table in sync.
"""

from __future__ import annotations

from typing import Iterable


def join_names(names: Iterable[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def connecting(agent: str) -> str:
    return f"Connecting you to {agent} now."


def connecting_indirect(agent: str) -> str:
    return f"Getting {agent} for that. Say no if that's not right."


INDIRECT_CANCELLED = "Okay. Who should I ask instead?"


def did_you_mean(names: list[str]) -> str:
    return f"Did you mean {join_names(names[:3])}?"


def unknown_agent(name: str) -> str:
    return f"I couldn't find an agent called {name}."


NO_AGENT_FOR_REQUEST = "I don't have an agent that can do that."


def unreachable(agent: str) -> str:
    return f"{agent} isn't answering right now."


def task_only(agent: str) -> str:
    return f"{agent} doesn't take live calls, but I can send it a task."


def bridge_only(agent: str) -> str:
    return f"{agent} only takes live calls. Want me to connect you?"


ASK_CONNECT_OR_DISPATCH = "Should I connect you, or have it done and let you know?"


def ask_check_agent(agent: str) -> str:
    return f"Want me to check {agent}?"


def ask_for_slot(slot: str, question: str = "") -> str:
    if question:
        return question
    label = slot.replace("_", " ").strip() or "that"
    return f"For what {label}?"


def which_value(values: list[str]) -> str:
    return f"{join_names(values)}?"


def read_back(summary: str) -> str:
    summary = summary.rstrip(". ")
    return f"{summary}. Shall I go ahead?"


def dispatched(agent: str, *, will_tell: bool) -> str:
    base = f"Okay, I've asked {agent} to handle that."
    return f"{base} I'll let you know when it's done." if will_tell else base


TOO_MANY_TASKS = "You already have several tasks running. Want me to cancel one first?"
BUSY = "The agents are busy right now. Try again in a moment."


def agent_question(agent: str, question: str) -> str:
    return f"{agent} needs something from you: {question}"


def escalation_offer(agent: str) -> str:
    return f"{agent} has a few questions. Want me to put you through?"


def result(agent: str, say: str) -> str:
    say = say.strip()
    return f"{agent} finished: {say}" if say else f"{agent} finished."


def failure(agent: str, error: str) -> str:
    error = error.strip()
    return f"{agent} couldn't complete that. {error}".strip()


def late_result(agent: str, say: str) -> str:
    say = say.strip()
    return f"{agent} finished after all: {say}" if say else f"{agent} finished after all."


def overdue(agent: str, intent: str, due: str) -> str:
    return f"{agent} still hasn't finished {intent}. It was due at {due}. Want me to cancel it?"


def timed_out(agent: str, intent: str, due: str) -> str:
    return f"{agent} didn't finish {intent} by {due}, so I cancelled it."


def lost(agent: str, intent: str) -> str:
    return f"{agent} lost track of {intent}."


def stalled(agent: str, intent: str) -> str:
    return f"{agent} hasn't checked in for a while, so {intent} may be stuck."


def more_updates(n: int) -> str:
    return f"You have {n} more task updates. Want to hear them?"


def cancelled(agent: str, intent: str) -> str:
    return f"Okay, I've cancelled {intent} with {agent}."


def cancel_confirm(intent: str) -> str:
    return f"Cancel {intent}? It may already be confirmed."


def completed_by_user(intent: str) -> str:
    return f"Okay, I've marked {intent} as done."


def deleted(intent: str) -> str:
    return f"Okay, I've removed {intent}."


def updated(agent: str) -> str:
    return f"Okay, I've told {agent} about the change."


def already_finished(agent: str, intent: str) -> str:
    return f"{agent} already finished {intent}."


def update_too_late(agent: str) -> str:
    return f"{agent} says it's too late to change that. Want me to cancel it instead?"


def answer_sent(agent: str) -> str:
    return f"Okay, I've passed that to {agent}."


NO_SUCH_TASK = "I don't see a task like that."
NO_PENDING_ACTION = "There's nothing waiting for a yes right now."
ACTION_DECLINED = "Okay, I won't do that."


def which_task(descriptions: list[str]) -> str:
    return f"The {join_names(descriptions[:3])}?"


def status_line(agent: str, intent: str, status: str, question: str = "") -> str:
    if status in ("pending", "dispatching"):
        return f"{agent} hasn't started {intent} yet."
    if status == "running":
        return f"{agent} is still working on {intent}."
    if status == "input_required":
        return f"{agent} is waiting for you: {question}" if question else f"{agent} is waiting for your answer."
    if status == "completed":
        return f"{agent} finished {intent}."
    if status == "failed":
        return f"{agent} couldn't complete {intent}."
    if status == "cancelled":
        return f"{intent} with {agent} was cancelled."
    if status == "timed_out":
        return f"{intent} with {agent} ran past its deadline and was cancelled."
    return f"{agent}: {intent} is {status}."


NO_TASKS = "You don't have any tasks right now."


def found_agents(agents: list[tuple[str, str]]) -> str:
    """`find_agents` reply: up to 3 (name, one-line description) pairs."""
    if not agents:
        return "I couldn't find an agent for that."
    parts = [f"{n}, {d}" if d else n for n, d in agents[:3]]
    lead = "I found one agent: " if len(parts) == 1 else f"I found {len(parts)} agents: "
    return lead + "; ".join(parts) + "."


ALREADY_CONNECTED = "You're already connected to an agent. Say stop to come back to me first."


def ask_for_answer(agent: str) -> str:
    return f"What should I tell {agent}?"


def not_waiting(agent: str) -> str:
    return f"{agent} isn't waiting for an answer right now."


def busy_named(agent: str) -> str:
    return f"{agent} is busy right now. Try again in a moment."


def cant_do(agent: str) -> str:
    return f"{agent} can't do that."


def agent_cancelled(agent: str, intent: str) -> str:
    return f"{agent} cancelled {intent}."


def declined_call(agent: str) -> str:
    return f"{agent} declined the call."


def delete_confirm(intent: str) -> str:
    return f"Delete {intent}? It may already be confirmed."


def cannot_meet_deadline(agent: str, message: str = "") -> str:
    why = f" {message.strip()}" if message.strip() else ""
    return f"{agent} can't finish that by the deadline.{why} Want me to try without a deadline?"


def update_failed(agent: str, message: str = "") -> str:
    return f"{agent} couldn't make that change. {message}".strip()


def which_to_cancel(lines: list[str]) -> str:
    return " ".join(lines) + " Which one should I cancel?"


ASK_WHAT_TO_CHANGE = "What should I change?"
OKAY = "Okay."
SOMETHING_WRONG_AGENT = "Sorry, something went wrong reaching that agent."
SOMETHING_WRONG_TASK = "Sorry, something went wrong with that task."
