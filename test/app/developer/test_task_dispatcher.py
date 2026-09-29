"""Tests for task_dispatcher.py (protocol v2 task mode) against real local WebSockets.

Each test spins up one or more tiny v2 agents with `websockets.serve` on
127.0.0.1 and routes to them through an `AgentRouter` fed synthetic registry
rows — no database, FastAPI or Pipecat required.

Run from repo root:

    python -m pytest test/app/developer/test_task_dispatcher.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(project_root, "app"))

import websockets  # noqa: E402

import agent_protocol as proto  # noqa: E402
import task_dispatcher as td  # noqa: E402
from agent_router import AgentRouter  # noqa: E402


class FakeAgent:
    """A scriptable v2 task-mode agent.

    behaviour: "ok" | "reject" | "reject_final" | "slow" | "ask" | "drop" |
               "no_task_mode" | "flood" | "mute" (never accepts)
    """

    def __init__(self, behaviour: str = "ok", *, max_concurrency: int = 8, work_s: float = 0.05):
        self.behaviour = behaviour
        self.max_concurrency = max_concurrency
        self.work_s = work_s
        self.dispatched: list[dict] = []
        self.cancelled: list[str] = []
        self.connections = 0
        self.peak_inflight = 0
        self._inflight = 0
        self.server = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/relay"

    async def start(self) -> "FakeAgent":
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws) -> None:
        self.connections += 1
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello" and hello["mode"] == "task", hello
        modes = ["bridge"] if self.behaviour == "no_task_mode" else ["bridge", "task"]
        await ws.send(json.dumps({
            "type": "ack", "accept": True, "service_id": "fake", "version": "2",
            "modes": modes, "max_concurrency": self.max_concurrency,
        }))
        lock = asyncio.Lock()
        answers: dict[str, asyncio.Queue] = {}
        workers: dict[str, asyncio.Task] = {}

        async def send(frame):
            async with lock:
                await ws.send(json.dumps(frame))

        async def work(msg):
            tid = msg["task_id"]
            self._inflight += 1
            self.peak_inflight = max(self.peak_inflight, self._inflight)
            try:
                if self.behaviour == "mute":
                    await asyncio.sleep(30)
                    return
                if self.behaviour == "reject":
                    await send({"type": "task.rejected", "task_id": tid, "reason": "nope", "retryable": True})
                    return
                if self.behaviour == "reject_final":
                    await send({"type": "task.rejected", "task_id": tid, "reason": "not allowed", "retryable": False})
                    return
                await send({"type": "task.accepted", "task_id": tid})
                if self.behaviour == "drop":
                    await ws.close()
                    return
                if self.behaviour == "slow":
                    await asyncio.sleep(30)
                if self.behaviour == "flood":
                    for i in range(1000):
                        await send({"type": "task.progress", "task_id": tid, "message": f"p{i}"})
                if self.behaviour == "ask":
                    answers[tid] = asyncio.Queue()
                    await send({"type": "task.input_required", "task_id": tid, "question": "Which city?"})
                    ans = await answers[tid].get()
                    await send({"type": "task.result", "task_id": tid, "status": "succeeded", "say": f"Booked in {ans}."})
                    return
                await send({"type": "task.progress", "task_id": tid, "message": "working"})
                await asyncio.sleep(self.work_s)
                await send({
                    "type": "task.result", "task_id": tid, "status": "succeeded",
                    "say": f"Done: {msg['intent']}", "output": {"n": 1},
                })
            except asyncio.CancelledError:
                pass
            finally:
                self._inflight -= 1

        try:
            async for raw in ws:
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "task.dispatch":
                    self.dispatched.append(msg)
                    workers[msg["task_id"]] = asyncio.create_task(work(msg))
                elif t == "task.cancel":
                    self.cancelled.append(msg["task_id"])
                    w = workers.get(msg["task_id"])
                    if w:
                        w.cancel()
                elif t == "task.input":
                    answers[msg["task_id"]].put_nowait(msg["answer"])
                elif t == "bye":
                    break
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            for w in workers.values():
                w.cancel()


def row(aid, name, url, *, modes=("bridge", "task"), desc="", keywords=(), max_concurrency=8):
    return {
        "id": aid, "name": name, "url": url, "description": desc,
        "keywords": list(keywords), "modes": list(modes), "max_concurrency": max_concurrency,
    }


async def wait_terminal(d: td.TaskDispatcher, task_id: str, timeout: float = 5.0) -> td.TaskRecord:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        rec = d.get(task_id)
        if rec is not None and rec.terminal:
            return rec
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} not terminal: {d.get(task_id)}")


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agents: list[FakeAgent] = []
        self.dispatchers: list[td.TaskDispatcher] = []

    async def asyncTearDown(self):
        for d in self.dispatchers:
            await d.shutdown()
        for a in self.agents:
            await a.stop()

    async def agent(self, behaviour="ok", **kw) -> FakeAgent:
        a = await FakeAgent(behaviour, **kw).start()
        self.agents.append(a)
        return a

    def dispatcher(self, rows, **kw) -> td.TaskDispatcher:
        router = AgentRouter(loader=lambda: rows, failure_threshold=2, cooldown_s=60)
        router.load_now()
        d = td.TaskDispatcher(router, **kw)
        self.dispatchers.append(d)
        return d

    async def test_named_agent_success_and_listener(self):
        a = await self.agent()
        d = self.dispatcher([row("a1", "Booker", a.url, desc="books restaurant tables")])
        seen = []

        async def listener(rec):
            seen.append(rec.status)

        d.subscribe("u1", listener)
        res = await d.submit(user_id="u1", intent="book a table for two", agent="booker")
        self.assertTrue(res.ok, res.to_public())
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.SUCCEEDED)
        self.assertEqual(rec.say, "Done: book a table for two")
        self.assertEqual(rec.output, {"n": 1})
        self.assertIn(td.ACCEPTED, seen)
        self.assertEqual(seen[-1], td.SUCCEEDED)
        self.assertEqual(a.dispatched[0]["user_id"], "u1")
        self.assertEqual(a.dispatched[0]["idempotency_key"], rec.task_id)

    async def test_intent_routing_without_a_name(self):
        a = await self.agent()
        b = await self.agent()
        d = self.dispatcher([
            row("a1", "Tabletop", a.url, desc="books restaurant tables", keywords=["restaurant", "reservation"]),
            row("b1", "Skyway", b.url, desc="books flights", keywords=["flight", "airline"]),
        ])
        res = await d.submit(user_id="u1", intent="reserve a restaurant for tonight")
        self.assertTrue(res.ok, res.to_public())
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.agent_name, "Tabletop")
        self.assertEqual(len(a.dispatched), 1)
        self.assertEqual(len(b.dispatched), 0)

    async def test_failover_on_retryable_rejection_for_intent_routing(self):
        bad = await self.agent("reject")
        good = await self.agent()
        d = self.dispatcher([
            row("bad", "Flights One", bad.url, desc="books flights", keywords=["flight"]),
            row("good", "Flights Two", good.url, desc="books flights", keywords=["flight"]),
        ])
        res = await d.submit(user_id="u1", intent="book a flight")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.SUCCEEDED)
        self.assertEqual(rec.agent_name, "Flights Two")
        self.assertEqual(rec.attempts, 2)

    async def test_accept_timeout_cancels_before_failover(self):
        mute = await self.agent("mute")
        good = await self.agent()
        router_rows = [
            row("mute", "Parcel One", mute.url, desc="ship parcel", keywords=["parcel"]),
            row("good", "Parcel Two", good.url, desc="ship parcel", keywords=["parcel"]),
        ]
        d = self.dispatcher(router_rows, accept_timeout_s=0.3)
        res = await d.submit(user_id="u1", intent="ship a parcel")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.SUCCEEDED)
        self.assertEqual(rec.agent_name, "Parcel Two")
        await asyncio.sleep(0.1)
        self.assertIn(rec.task_id, mute.cancelled)

    async def test_named_agent_is_not_substituted(self):
        bad = await self.agent("reject")
        other = await self.agent()
        d = self.dispatcher([
            row("bad", "Kairos", bad.url, desc="calendar"),
            row("other", "Chronos", other.url, desc="calendar"),
        ])
        res = await d.submit(user_id="u1", intent="move my 3pm", agent="kairos")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.FAILED)
        self.assertEqual(rec.tried_agents, ["Kairos"])
        self.assertEqual(len(other.dispatched), 0)

    async def test_non_retryable_rejection_stops(self):
        a = await self.agent("reject_final")
        b = await self.agent()
        d = self.dispatcher([
            row("a", "Pay One", a.url, desc="payments", keywords=["pay"]),
            row("b", "Pay Two", b.url, desc="payments", keywords=["pay"]),
        ])
        res = await d.submit(user_id="u1", intent="pay my bill")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.FAILED)
        self.assertIn("not allowed", rec.error)
        self.assertEqual(rec.attempts, 1)

    async def test_connect_failure_fails_over_and_opens_circuit(self):
        good = await self.agent()
        dead_url = "ws://127.0.0.1:1/relay"  # nothing listens on port 1
        d = self.dispatcher([
            row("dead", "Weather Alpha", dead_url, desc="weather forecast", keywords=["weather"]),
            row("good", "Weather Beta", good.url, desc="weather forecast", keywords=["weather"]),
        ])
        for _ in range(2):
            res = await d.submit(user_id="u1", intent="weather forecast")
            rec = await wait_terminal(d, res.task.task_id)
            self.assertEqual(rec.status, td.SUCCEEDED)
        self.assertFalse(d.router.is_healthy("dead"))
        # Circuit open → the healthy agent is tried first, no wasted dial.
        res = await d.submit(user_id="u1", intent="weather forecast")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.attempts, 1)
        self.assertEqual(rec.agent_name, "Weather Beta")

    async def test_input_required_round_trip(self):
        a = await self.agent("ask")
        d = self.dispatcher([row("a", "Hotel Desk", a.url, desc="hotels")])
        res = await d.submit(user_id="u1", intent="book a hotel", agent="hotel desk")
        tid = res.task.task_id
        for _ in range(100):
            if d.get(tid).status == td.INPUT_REQUIRED:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(d.get(tid).status, td.INPUT_REQUIRED)
        self.assertEqual(d.get(tid).question, "Which city?")
        self.assertIn("Which city?", d.get(tid).spoken_summary())
        self.assertTrue(await d.provide_input(tid, "Lisbon"))
        rec = await wait_terminal(d, tid)
        self.assertEqual(rec.say, "Booked in Lisbon.")

    async def test_deadline_times_out_and_cancels_on_agent(self):
        a = await self.agent("slow")
        d = self.dispatcher([row("a", "Slowpoke", a.url)])
        res = await d.submit(user_id="u1", intent="do it", agent="slowpoke", deadline_s=1.0)
        rec = await wait_terminal(d, res.task.task_id, timeout=5)
        self.assertEqual(rec.status, td.TIMED_OUT)
        await asyncio.sleep(0.1)
        self.assertIn(rec.task_id, a.cancelled)

    async def test_user_cancel(self):
        a = await self.agent("slow")
        d = self.dispatcher([row("a", "Slowpoke", a.url)])
        res = await d.submit(user_id="u1", intent="do it", agent="slowpoke")
        await asyncio.sleep(0.2)
        rec = await d.cancel(res.task.task_id)
        self.assertEqual(rec.status, td.CANCELLED)
        await asyncio.sleep(0.1)
        self.assertIn(rec.task_id, a.cancelled)

    async def test_disconnect_after_accept_is_not_retried(self):
        a = await self.agent("drop")
        b = await self.agent()
        d = self.dispatcher([
            row("a", "Taxi One", a.url, desc="taxi", keywords=["taxi"]),
            row("b", "Taxi Two", b.url, desc="taxi", keywords=["taxi"]),
        ])
        res = await d.submit(user_id="u1", intent="get me a taxi")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.FAILED)
        self.assertIn("mid-task", rec.error)
        self.assertEqual(len(b.dispatched), 0)

    async def test_agent_without_task_mode_is_not_a_candidate(self):
        a = await self.agent()
        d = self.dispatcher([row("a", "Radio", a.url, modes=("bridge",))])
        res = await d.submit(user_id="u1", intent="play jazz", agent="radio")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, td.REASON_WRONG_MODE)

    async def test_ack_without_task_mode_fails_cleanly(self):
        a = await self.agent("no_task_mode")
        d = self.dispatcher([row("a", "Liar", a.url)])  # registry claims task, ack disagrees
        res = await d.submit(user_id="u1", intent="x", agent="liar")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.FAILED)
        self.assertIn("task mode", rec.error)

    async def test_ambiguous_named_agent_asks(self):
        a = await self.agent()
        d = self.dispatcher([
            row("a", "Weather Bot", a.url),
            row("b", "Weather Bat", a.url),
        ])
        res = await d.submit(user_id="u1", intent="forecast", agent="weather b")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, td.REASON_AMBIGUOUS)
        self.assertGreaterEqual(len(res.route.candidates), 2)

    async def test_no_agent(self):
        a = await self.agent()
        d = self.dispatcher([row("a", "Weather Bot", a.url, desc="weather")])
        res = await d.submit(user_id="u1", intent="file my taxes", agent="xyzzy plugh")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, td.REASON_NO_AGENT)

    async def test_per_agent_concurrency_is_respected_and_connection_shared(self):
        a = await self.agent(max_concurrency=2, work_s=0.2)
        d = self.dispatcher([row("a", "Batcher", a.url, max_concurrency=2)])
        ids = []
        for i in range(6):
            res = await d.submit(user_id=f"user{i}", intent=f"job {i}", agent="batcher")
            self.assertTrue(res.ok)
            ids.append(res.task.task_id)
        recs = [await wait_terminal(d, t, timeout=10) for t in ids]
        self.assertTrue(all(r.status == td.SUCCEEDED for r in recs))
        self.assertLessEqual(a.peak_inflight, 2)
        self.assertEqual(a.connections, 1)  # one multiplexed socket for all six

    async def test_per_user_limit(self):
        a = await self.agent("slow")
        d = self.dispatcher([row("a", "Slowpoke", a.url)], max_active_per_user=2)
        for _ in range(2):
            self.assertTrue((await d.submit(user_id="u1", intent="x", agent="slowpoke")).ok)
        res = await d.submit(user_id="u1", intent="x", agent="slowpoke")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, td.REASON_USER_LIMIT)
        # Another user is unaffected.
        self.assertTrue((await d.submit(user_id="u2", intent="x", agent="slowpoke")).ok)

    async def test_undelivered_results_are_kept_for_reconnect(self):
        a = await self.agent()
        d = self.dispatcher([row("a", "Booker", a.url)])
        res = await d.submit(user_id="ghost", intent="x", agent="booker")
        await wait_terminal(d, res.task.task_id)
        pending = d.drain_undelivered("ghost")
        self.assertEqual([r.task_id for r in pending], [res.task.task_id])
        self.assertEqual(d.drain_undelivered("ghost"), [])

    async def test_progress_flood_does_not_block_result(self):
        a = await self.agent("flood")
        d = self.dispatcher([row("a", "Chatty", a.url)])
        res = await d.submit(user_id="u1", intent="x", agent="chatty")
        rec = await wait_terminal(d, res.task.task_id, timeout=10)
        self.assertEqual(rec.status, td.SUCCEEDED)
        self.assertLessEqual(len(rec.progress), 20)

    async def test_idle_connections_are_reaped(self):
        a = await self.agent()
        pool = td.ConnectionPool(idle_close_s=0.05)
        d = self.dispatcher([row("a", "Booker", a.url)], pool=pool)
        res = await d.submit(user_id="u1", intent="x", agent="booker")
        rec = await wait_terminal(d, res.task.task_id)
        self.assertEqual(rec.status, td.SUCCEEDED, rec.to_public())
        self.assertEqual(len(pool), 1)
        await asyncio.sleep(0.1)
        self.assertEqual(await pool.reap_idle(), 1)
        self.assertEqual(len(pool), 0)


class ProtocolTests(unittest.TestCase):
    def test_v1_ack_means_bridge_only(self):
        self.assertEqual(proto.ack_modes({"type": "ack", "accept": True, "version": "1"}), ["bridge"])
        self.assertEqual(proto.ack_modes({"modes": ["Task", "bridge"]}), ["task", "bridge"])

    def test_parse_rejects_non_objects(self):
        self.assertIsNone(proto.parse(b"\x00"))
        self.assertIsNone(proto.parse("[1,2]"))
        self.assertIsNone(proto.parse("not json"))
        self.assertEqual(proto.parse('{"type":"x"}'), {"type": "x"})

    def test_hello_is_v2_and_carries_mode(self):
        h = proto.hello("u", mode="task")
        self.assertEqual((h["type"], h["version"], h["mode"]), ("hello", "2", "task"))
        self.assertTrue(h["session_id"].startswith("sess_"))

    def test_clip(self):
        self.assertEqual(len(proto.clip_text("x" * 5000)), proto.MAX_SAY_CHARS)


if __name__ == "__main__":
    unittest.main()
