"""Voice tool handlers vs the decision matrix (ORCHESTRATOR_V2_TOOL_CALLS.md §1.2).

Drives the real `SpeechPipeline` handlers (route_to_agent, manage_task,
find_agents, task announcements) with fake audio / LLM / bridge objects and a
real TaskService, Postgres and reference agent. Each test names the matrix row
it covers. Needs `pgserver`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import pgfix  # noqa: E402

from test_service import ServiceTestBase, wait_for  # noqa: E402

import agents_registry  # noqa: E402
from orchestrator.bridge import OUTCOME_CONNECT_FAILED, OUTCOME_OK, BridgeStartResult  # noqa: E402
from orchestrator.pipeline import SpeechPipeline  # noqa: E402
from orchestrator.tasks.service import Announcement  # noqa: E402


class FakeLLM:
    def __init__(self):
        self.turns: list[str] = []
        self.announcements: list[str] = []
        self.answered: list[str] = []
        self.turn_filter = None

    def recent_user_texts(self, n=3):
        return self.turns[-n:]

    def add_assistant_announcement(self, text):
        self.announcements.append(text)

    async def answer_directly(self, hint):
        self.answered.append(hint)

    def set_turn_filter(self, fn):
        self.turn_filter = fn

    async def push_frame(self, frame):
        self.frames.append(frame)


class FakeParams:
    def __init__(self, args, sink):
        self.arguments = args
        self.llm = sink
        self.result = None

    async def result_callback(self, result, properties=None):
        self.result = result


class FakeSink:
    def __init__(self):
        self.frames = []

    async def push_frame(self, frame):
        self.frames.append(frame)

    async def queue_frame(self, frame):
        self.frames.append(frame)


class FakeBridge:
    def __init__(self):
        self.active = False
        self.calls = []
        self.outcome = OUTCOME_OK
        self.agent_id = ""
        self.agent_name = ""

    async def start(self, url, **kw):
        self.calls.append((url, kw))
        if self.outcome == OUTCOME_OK:
            self.active = True
            return BridgeStartResult(ok=True, outcome=OUTCOME_OK, service_id="x")
        return BridgeStartResult(ok=False, outcome=self.outcome, detail="boom")


class FakeAudio:
    def __init__(self):
        self.audible_until = 0.0

    def is_bot_audible(self):
        return time.monotonic() < self.audible_until


class FakeWS:
    from starlette.websockets import WebSocketState as _S

    client_state = _S.CONNECTED


class PipelineToolTests(ServiceTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        mk = agents_registry.create_agent
        # Live calls only here (production Kairos also takes tasks; see test_kairos_tasks).
        await asyncio.to_thread(lambda: mk(name="Kairos", url="wss://kairos/ws/{user_id}",
                                           extra={"modes": ["bridge"]}))
        await asyncio.to_thread(lambda: mk(name="MyFitnessPal", url="ws://mfp/relay"))
        await asyncio.to_thread(lambda: mk(name="Atlas", url="ws://atlas", description="Maps"))
        await asyncio.to_thread(lambda: mk(name="Atlas Travel", url="ws://atlas-t", description="Flights"))
        await asyncio.to_thread(lambda: mk(
            name="Ledger", url=self.agent_row["url"], description="Accounting",
            extra={"modes": ["task"], "events": ["callback"]}))
        await asyncio.to_thread(lambda: agents_registry.update_agent(self.agent_row["id"], {"slots": [
            {"name": "restaurant", "required": True},
            {"name": "party_size", "required": True, "question": "For how many people?"},
            {"name": "time", "required": False}]}))
        self.router.load_now()
        p = SpeechPipeline.__new__(SpeechPipeline)
        p._user_id = self.user
        p._service = self.svc
        p._pending = None
        p._last_task_id = None
        p._contexts = {}
        p._held = []
        p._announce_lock = asyncio.Lock()
        p._indirect_window_s = 0.3
        p._user_spoke_at = 0.0
        p._llm = FakeLLM()
        p._bridge = FakeBridge()
        p._audio = FakeAudio()
        p._ws = FakeWS()
        self.sink = FakeSink()
        p._task = self.sink
        self.p = p

    def said(self):
        from pipecat.frames.frames import TTSSpeakFrame

        return [f.text for f in self.sink.frames if isinstance(f, TTSSpeakFrame)]

    async def route(self, user_text, **args):
        self.p._llm.turns.append(user_text)
        params = FakeParams(args, self.sink)
        await self.p._handle_route_to_agent(params)
        return params.result

    async def manage(self, user_text, **args):
        self.p._llm.turns.append(user_text)
        params = FakeParams(args, self.sink)
        await self.p._handle_manage_task(params)
        return params.result

    # Row 1 / 8: no agent needed → Gemini answers itself.
    async def test_row8_on_request_general_question_is_answered(self):
        r = await self.route("how many calories are in a banana", intent="how many calories are in a banana",
                             mode_hint="auto")
        self.assertEqual(r["outcome"], "answered")
        self.assertTrue(self.p._llm.answered)
        self.assertEqual(self.p._bridge.calls, [])

    # Row 2: named, garbled → connect with the fixed line.
    async def test_row2_named_garble_connects(self):
        r = await self.route("can i speak to cairo's", agent="cairo's", intent="talk to kairos", mode_hint="connect")
        self.assertEqual(r["outcome"], "connected")
        self.assertEqual(self.said()[0], "Connecting you to Kairos now.")
        url, kw = self.p._bridge.calls[0]
        self.assertEqual(url, f"wss://kairos/ws/{self.user}")
        self.assertTrue(kw["context_id"].startswith("c-"))

    async def test_row3_ambiguous_asks(self):
        r = await self.route("call atlas", agent="Atlas", intent="talk to atlas", mode_hint="connect")
        self.assertEqual(r["outcome"], "connected")  # exact name beats the prefix sibling
        r = await self.route("get me the atlas thing", agent="atl", intent="talk", mode_hint="connect")
        self.assertEqual(r["outcome"], "ambiguous")
        self.assertEqual(self.said()[-1], "Did you mean Atlas or Atlas Travel?")

    async def test_row4_unknown_named_agent_declined(self):
        r = await self.route("ask zorblax", agent="Zorblax", intent="ask zorblax", mode_hint="auto")
        self.assertEqual(r["outcome"], "unknown_agent")
        self.assertEqual(self.said(), ["I couldn't find an agent called Zorblax."])
        self.assertEqual(self.p._bridge.calls, [])

    async def test_row5_unreachable_named_agent(self):
        self.p._bridge.outcome = OUTCOME_CONNECT_FAILED
        r = await self.route("talk to kairos", agent="Kairos", intent="talk to kairos", mode_hint="connect")
        self.assertEqual(r["outcome"], "failed")
        self.assertEqual(self.said()[-1], "Kairos isn't answering right now.")

    async def test_row6_wrong_mode_offer_then_confirm(self):
        r = await self.route("talk to ledger", agent="Ledger", intent="review my expenses", mode_hint="connect")
        self.assertEqual(r["outcome"], "wrong_mode")
        self.assertEqual(self.said()[-1], "Ledger doesn't take live calls, but I can send it a task.")
        r = await self.manage("yes do that", action="confirm")
        self.assertEqual(r["outcome"], "read_back")  # side_effects defaults to true
        self.assertEqual(self.said()[-1], "Review my expenses, with Ledger. Shall I go ahead?")
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "dispatched")
        self.assertEqual(self.said()[-1], "Okay, I've asked Ledger to handle that. I'll let you know when it's done.")

    async def test_row7_indirect_route_with_cancel_cue(self):
        r = await self.route("what's on my list today", intent="what's on my list today", mode_hint="auto")
        self.assertEqual(r["outcome"], "connected")
        self.assertEqual(self.said()[0], "Getting Kairos for that. Say no if that's not right.")
        self.assertEqual(len(self.p._bridge.calls), 1)

    async def test_row7_agent_filled_in_by_gemini_is_still_indirect(self):
        # Seen with the real model: Gemini names the agent the user never said.
        r = await self.route("what's on my list today", agent="Kairos", intent="what's on my list today",
                             mode_hint="auto")
        self.assertEqual(r["outcome"], "connected")
        self.assertEqual(self.said()[0], "Getting Kairos for that. Say no if that's not right.")

    async def _user_says(self, text, delay=0.1):
        await asyncio.sleep(delay)
        self.p._user_spoke_at = time.monotonic()
        await asyncio.sleep(0.05)
        swallowed = self.p._llm.turn_filter(text)  # the transcript reaches the LLM service
        self.swallowed = swallowed

    async def test_row7_indirect_route_cancelled_by_user(self):
        asyncio.create_task(self._user_says("no, not that"))
        r = await self.route("what's on my list", intent="what's on my list", mode_hint="auto")
        self.assertEqual(r["outcome"], "cancelled_by_user")
        self.assertEqual(self.said()[-1], "Okay. Who should I ask instead?")
        self.assertEqual(self.p._bridge.calls, [])
        self.assertTrue(self.swallowed)

    async def test_row7_yes_during_window_still_connects(self):
        asyncio.create_task(self._user_says("yes please"))
        r = await self.route("what's on my list", intent="what's on my list", mode_hint="auto")
        self.assertEqual(r["outcome"], "connected")
        self.assertTrue(self.swallowed)

    async def test_row7_new_request_during_window_supersedes(self):
        asyncio.create_task(self._user_says("actually what's the weather"))
        r = await self.route("what's on my list", intent="what's on my list", mode_hint="auto")
        self.assertEqual(r["outcome"], "superseded")
        self.assertEqual(self.p._bridge.calls, [])
        self.assertFalse(self.swallowed)  # Gemini answers the new request

    async def test_too_many_tasks_then_yes_lists_tasks(self):
        for _ in range(8):
            await self.dispatch(intent="never finish", slots={})
        await self.route("have tabletop book nopa for two", agent="Tabletop", intent="book Nopa for 2",
                         mode_hint="dispatch", slots=[{"name": "restaurant", "value": "Nopa"},
                                                      {"name": "party_size", "value": "2"}])
        await self.manage("yes", action="confirm")
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "ask_which")
        self.assertTrue(self.said()[-1].endswith("Which one should I cancel?"))

    async def test_offer_cancel_from_announcement(self):
        t = await self.dispatch(intent="never finish", slots={})
        ann = Announcement(task_id=t["task_id"], user_id=self.user, agent_name="Tabletop",
                           line="Tabletop still hasn't finished it. Want me to cancel it?", kind="question",
                           terminal=False, offer="cancel")
        await self.p._on_task_announcement(ann)
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "cancel_now")
        self.assertEqual((await self.svc.get(t["task_id"]))["status"], "cancelled")

    async def test_update_needing_recreate_reads_back(self):
        await asyncio.to_thread(lambda: agents_registry.update_agent(
            self.agent_row["id"], {"task_ops": ["dispatch", "cancel", "input"]}))
        self.router.load_now()
        t = await self.dispatch(intent="never finish", slots={"time": "19:00"})
        await wait_for(lambda: self._running(t["task_id"]))
        r = await self.manage("make it eight instead", action="update", task_ref="that",
                              changes=[{"name": "time", "value": "20:00"}])
        self.assertEqual(r["outcome"], "read_back")
        self.assertTrue(self.said()[-1].endswith("Shall I go ahead?"))
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "recreate")
        self.assertEqual(self.said()[-1], "Okay, I've told Tabletop about the change.")

    async def _running(self, tid):
        return (await self.svc.get(tid))["status"] == "running"

    async def test_more_updates_offer(self):
        for i in range(5):
            t = await self.dispatch(intent=f"job {i}", slots={})
            await wait_for(lambda: self._is_done(t["task_id"]))
        await self.p._announce_session_start()
        self.assertEqual(self.said()[-1], "You have 2 more task updates. Want to hear them?")
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "more_updates")
        self.assertEqual(len([x for x in self.said() if x.startswith("Tabletop finished")]), 5)

    async def _is_done(self, tid):
        return (await self.svc.get(tid))["status"] == "completed"

    async def test_held_updates_survive_session_close(self):
        self.p._bridge.active = True
        self.p._unsubscribe = None
        t = await self.dispatch(intent="never finish", slots={})
        ann = Announcement(task_id=t["task_id"], user_id=self.user, agent_name="Tabletop",
                           line="Tabletop needs something from you: Which day?", kind="question", terminal=False)
        await self.p._on_task_announcement(ann)
        self.p._bridge.close = lambda *a, **k: asyncio.sleep(0)
        self.p._task.cancel = lambda **k: asyncio.sleep(0)
        self.p._runner_task = None
        self.p._audio.shutdown_playback = lambda: asyncio.sleep(0)
        await self.p.close()
        items = await self.svc.session_start(self.user)
        self.assertEqual(items[0][1], "Tabletop needs something from you: Which day?")

    async def test_row10_missing_required_slot_asks(self):
        r = await self.route("have tabletop book nopa", agent="Tabletop", intent="book a table at nopa",
                             mode_hint="dispatch", slots=[{"name": "restaurant", "value": "Nopa"}])
        self.assertEqual((r["outcome"], r["slot"]), ("ask", "party_size"))
        self.assertEqual(self.said()[-1], "For how many people?")

    async def test_row11_invented_value_asks(self):
        r = await self.route("have tabletop book nopa for two", agent="Tabletop", intent="book nopa for 2",
                             mode_hint="dispatch", slots=[{"name": "restaurant", "value": "Nopa"},
                                                          {"name": "party_size", "value": "2"},
                                                          {"name": "time", "value": "19:00"}])
        self.assertEqual((r["outcome"], r["slot"], r["reason"]), ("ask", "time", "ungrounded"))
        self.assertEqual(self.said()[-1], "For what time?")

    async def test_row12_self_correction_asks(self):
        r = await self.route("have tabletop book nopa for two at seven, no, eight", agent="Tabletop",
                             intent="book nopa", mode_hint="dispatch",
                             slots=[{"name": "restaurant", "value": "Nopa"}, {"name": "party_size", "value": "2"},
                                    {"name": "time", "value": "19:00"}])
        self.assertEqual(r["reason"], "correction")
        self.assertEqual(self.said()[-1], "7 or eight?")

    async def test_row13_read_back_then_dispatch_and_result(self):
        r = await self.route("have tabletop book nopa for two at seven and let me know", agent="Tabletop",
                             intent="book Nopa for 2 at 7", mode_hint="dispatch", notify="device",
                             slots=[{"name": "restaurant", "value": "Nopa"}, {"name": "party_size", "value": "2"},
                                    {"name": "time", "value": "19:00"}])
        self.assertEqual(r["outcome"], "read_back")
        self.assertEqual(self.said()[-1], "Book Nopa for 2 at 7, with Tabletop. Shall I go ahead?")
        self.p._unsubscribe = self.svc.subscribe(self.user, self.p._on_task_announcement)
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "dispatched")
        self.assertEqual(self.said()[-1], "Okay, I've asked Tabletop to handle that. I'll let you know when it's done.")
        task = await self.svc.get(r["task_id"])
        self.assertEqual(task["notify"], "device")
        await wait_for(lambda: any(s.startswith("Tabletop finished") for s in self.said()))
        await wait_for(lambda: self._delivered(r["task_id"]))

    async def _delivered(self, tid):
        return (await self.svc.get(tid))["delivered_at"] is not None

    async def test_declined_read_back(self):
        await self.route("have tabletop book nopa for two", agent="Tabletop", intent="book Nopa for 2",
                         mode_hint="dispatch", slots=[{"name": "restaurant", "value": "Nopa"},
                                                      {"name": "party_size", "value": "2"}])
        r = await self.manage("no", action="decline")
        self.assertEqual(r["outcome"], "declined")
        self.assertEqual(self.said()[-1], "Okay, I won't do that.")
        self.assertEqual(await self.svc.list(self.user), [])

    async def test_notify_not_asked_is_not_device(self):
        await self.route("have tabletop book nopa for two", agent="Tabletop", intent="book Nopa for 2",
                         mode_hint="dispatch", notify="device",
                         slots=[{"name": "restaurant", "value": "Nopa"}, {"name": "party_size", "value": "2"}])
        r = await self.manage("yes", action="confirm")
        self.assertEqual((await self.svc.get(r["task_id"]))["notify"], "next_session")

    async def test_row14_user_limit(self):
        for _ in range(8):
            await self.dispatch(intent="never finish", slots={})
        await self.route("have tabletop book nopa for two", agent="Tabletop", intent="book Nopa for 2",
                         mode_hint="dispatch", slots=[{"name": "restaurant", "value": "Nopa"},
                                                      {"name": "party_size", "value": "2"}])
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "user_limit")
        self.assertEqual(self.said()[-1], "You already have several tasks running. Want me to cancel one first?")

    async def test_row16_task_updates_held_during_live_call(self):
        self.p._bridge.active = True
        t = await self.dispatch()
        ann = Announcement(task_id=t["task_id"], user_id=self.user, agent_name="Tabletop",
                           line="Tabletop finished: done", kind="result", terminal=True)
        self.assertTrue(await self.p._on_task_announcement(ann))
        self.assertEqual(self.said(), [])
        self.p._bridge.active = False
        await self.p._flush_held()
        self.assertEqual(self.said(), ["Tabletop finished: done"])

    async def test_manage_status_cancel_confirm_and_references(self):
        t1 = await self.dispatch(intent="never finish the dinner booking", slots={})
        t2 = await self.dispatch(intent="never finish the taxi booking", slots={})
        await wait_for(lambda: self._both_running(t1["task_id"], t2["task_id"]))
        r = await self.manage("are they done", action="status")
        self.assertEqual(r["count"], 2)
        r = await self.manage("cancel the booking", action="cancel", task_ref="the booking")
        self.assertEqual(r["outcome"], "no_task")
        self.assertTrue(self.said()[-1].startswith("The never finish the"))
        r = await self.manage("cancel the taxi", action="cancel", task_ref="the taxi")
        self.assertEqual(r["outcome"], "confirm")
        self.assertEqual(self.said()[-1], "Cancel your request to never finish the taxi booking? It may already be confirmed.")
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "cancel")
        self.assertEqual((await self.svc.get(t2["task_id"]))["status"], "cancelled")
        self.assertEqual((await self.svc.get(t1["task_id"]))["status"], "running")
        r = await self.manage("cancel the zebra", action="cancel", task_ref="the zebra")
        self.assertEqual(self.said()[-1], "I don't see a task like that.")

    async def _both_running(self, a, b):
        statuses = [(await self.svc.get(x))["status"] for x in (a, b)]
        return statuses == ["running", "running"]

    async def test_answer_goes_to_waiting_task(self):
        self.p._unsubscribe = self.svc.subscribe(self.user, self.p._on_task_announcement)
        t = await self.dispatch(intent="confirm the booking", slots={})
        await wait_for(lambda: "Tabletop needs something from you: Which restaurant?" in self.said())
        r = await self.manage("nopa", action="answer", answer="Nopa")
        self.assertEqual(r["outcome"], "answered")
        await wait_for(lambda: any("(nopa)" in s.lower() for s in self.said()))
        self.assertEqual((await self.svc.get(t["task_id"]))["status"], "completed")

    async def test_find_agents(self):
        params = FakeParams({"query": "restaurant reservations"}, self.sink)
        await self.p._handle_find_agents(params)
        self.assertTrue(self.said()[-1].startswith("I found one agent: Tabletop"))

    async def test_confirm_with_nothing_pending(self):
        r = await self.manage("yes", action="confirm")
        self.assertEqual(r["outcome"], "nothing_pending")
        self.assertEqual(self.said()[-1], "There's nothing waiting for a yes right now.")


if __name__ == "__main__":
    unittest.main()
