"""TaskService end to end: Postgres + the reference agent + real HTTP callbacks.

Covers ORCHESTRATOR_V2_TOOL_CALLS.md §1.5–1.7: dispatch/ack, nacks and
failover, events and dedupe, delivery (live, next session, device wake and
its limits), CRUD, deadlines, outbox resends, heartbeat reconciliation and
stall detection, and the HTTP auth rules.

Needs `pgserver` (embedded Postgres). Run from repo root:

    python -m pytest test/app/orchestrator/test_service.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import unittest
from datetime import timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import pgfix  # noqa: E402  (sets sys.path to app/)

os.environ["ORCHESTRATOR_QUIET_HOURS"] = "0-0"  # never quiet unless a test says so

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import agents_registry  # noqa: E402
from orchestrator.testing.task_agent import TaskAgent  # noqa: E402
from orchestrator.routing import embeddings
from orchestrator.tasks import link as link_mod, protocol as proto  # noqa: E402
from orchestrator.tasks import routes as orch_routes  # noqa: E402
from orchestrator.tasks import service as service_mod  # noqa: E402
from orchestrator.tasks.link import AgentLink  # noqa: E402
from orchestrator.routing.router import AgentRouter, set_router  # noqa: E402
from orchestrator.tasks.service import TaskService, set_service  # noqa: E402
from orchestrator.tasks.store import TaskStore, utcnow  # noqa: E402
from routes.agent_routes import router as agent_routes  # noqa: E402

link_mod.RETRY_BASE_S = 0.2
service_mod.RECONCILE_GRACE_S = 0.0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def wait_for(pred, timeout=8.0, step=0.05):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        v = pred()
        if asyncio.iscoroutine(v):
            v = await v
        if v:
            return v
        await asyncio.sleep(step)
    raise AssertionError("condition not met in time")


class ServiceTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pgfix.fresh_database()
        embeddings.set_embedder(None)
        os.environ["AGENT_ROUTER_EMBEDDINGS"] = "0"
        self.user = pgfix.add_user()
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.router = AgentRouter(refresh_s=0.0)
        set_router(self.router)
        self.store = TaskStore()
        self.link = AgentLink(self.store, self.router, sweep_s=0.2, idle_close_s=60)
        self.svc = TaskService(self.store, self.router, self.link, public_base=self.base)
        set_service(self.svc)
        app = FastAPI()
        app.include_router(orch_routes.router)
        app.include_router(agent_routes)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    timeout_graceful_shutdown=1))
        self.server_task = asyncio.create_task(self.server.serve())
        await wait_for(lambda: self.server.started)
        await self.svc.start()
        self.agent = TaskAgent(service_id="tabletop-test", resend_first_s=0.05, resend_max_s=0.5, resend_for_s=20)
        url = await self.agent.start()
        self.agent_row = await asyncio.to_thread(lambda: agents_registry.create_agent(
            name="Tabletop", url=url, description="Restaurant reservations",
            service_id="tabletop-test",
            extra={"modes": ["task", "bridge"], "events": ["callback", "ws"],
                   "task_ops": list(self.agent.task_ops), "max_reply_latency_s": 1, "side_effects": True,
                   "slots": [{"name": "restaurant", "required": False}]},
        ))
        self.router.load_now()
        self.heard: list[service_mod.Announcement] = []

    async def asyncTearDown(self):
        await self.svc.stop()
        await self.agent.stop()
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.server_task, timeout=5)
        except asyncio.TimeoutError:  # a lingering keep-alive connection: don't hang the suite
            self.server.force_exit = True
            await asyncio.wait_for(self.server_task, timeout=5)
        set_service(None)
        set_router(None)

    def listen(self, result=True):
        async def listener(ann):
            self.heard.append(ann)
            return result

        return self.svc.subscribe(self.user, listener)

    async def dispatch(self, intent="book nopa for 2 at 7", **kw):
        agent = self.router.get(self.agent_row["id"])
        kw.setdefault("slots", {"restaurant": "Nopa", "party_size": 2})
        out = await self.svc.dispatch(user_id=self.user, agent=agent, intent=intent, **kw)
        self.assertTrue(out.ok, out.reason)
        return out.task

    async def status(self, task_id):
        return (await self.svc.get(task_id))["status"]


class DispatchTests(ServiceTestBase):
    async def test_dispatch_ack_event_and_delivery(self):
        self.listen()
        task = await self.dispatch()
        tid = task["task_id"]
        self.assertEqual(task["status"], "pending")
        await wait_for(lambda: self.heard)
        self.assertEqual(await self.status(tid), "completed")
        ann = self.heard[0]
        self.assertTrue(ann.terminal)
        self.assertTrue(ann.line.startswith("Tabletop finished: Done: book nopa"))
        row = await self.svc.get(tid)
        self.assertEqual(row["task_info"]["agent_task_ref"], f"ref-{tid[:6]}")
        self.assertEqual(row["task_info"]["contextId"][:2], "c-")
        await self.svc.mark_delivered(tid, proto.DELIVERED_LIVE)
        await wait_for(lambda: self.agent.tasks[tid].delivered_via == "live")
        self.assertIsNotNone((await self.svc.get(tid))["delivered_at"])

    async def test_dispatch_carries_slots_context_and_callback(self):
        task = await self.dispatch(context_id="c-fixed")
        await wait_for(lambda: task["task_id"] in self.agent.tasks)
        t = self.agent.tasks[task["task_id"]]
        self.assertEqual(t.slots, {"restaurant": "Nopa", "party_size": 2})
        self.assertEqual(t.context_id, "c-fixed")
        self.assertTrue(t.callback["url"].startswith(self.base + "/developer/tasks/"))

    async def test_missing_input_nack_asks_then_redispatches_same_task(self):
        self.listen()
        task = await self.dispatch(intent="book a table at nopa", slots={"restaurant": "Nopa"})
        tid = task["task_id"]
        await wait_for(lambda: self.heard)
        self.assertEqual(self.heard[0].line, "How many people?")
        self.assertEqual(self.heard[0].kind, "question")
        line = await self.svc.answer(await self.svc.get(tid), "2")
        self.assertIn("passed that to Tabletop", line)
        await wait_for(lambda: len(self.heard) >= 2)
        self.assertEqual(await self.status(tid), "completed")
        self.assertEqual(self.agent.dispatch_count[tid], 2)
        self.assertEqual(self.agent.tasks[tid].slots["party_size"], "2")

    async def test_input_required_round_trip(self):
        self.listen()
        task = await self.dispatch(intent="confirm a booking")
        tid = task["task_id"]
        await wait_for(lambda: self.heard)
        self.assertEqual(self.heard[0].line, "Tabletop needs something from you: Which restaurant?")
        self.assertEqual(await self.status(tid), "input_required")
        await self.svc.answer(await self.svc.get(tid), "Nopa")
        await wait_for(lambda: len(self.heard) >= 2)
        self.assertIn("(nopa)", self.heard[1].line.lower())
        self.assertEqual(await self.status(tid), "completed")

    async def test_second_question_offers_live_call(self):
        self.listen()
        task = await self.dispatch(intent="twice confirm")
        tid = task["task_id"]
        await wait_for(lambda: self.heard)
        await self.svc.answer(await self.svc.get(tid), "Nopa")
        await wait_for(lambda: len(self.heard) >= 2)
        self.assertEqual(self.heard[1].line, "Tabletop has a few questions. Want me to put you through?")
        self.assertEqual(self.heard[1].escalate_agent_id, self.agent_row["id"])
        await self.svc.escalate(await self.svc.get(tid))
        await wait_for(lambda: self.agent.tasks[tid].cancelled)
        self.assertEqual(await self.status(tid), "cancelled")

    async def test_failed_result(self):
        self.listen()
        await self.dispatch(intent="fail please")
        await wait_for(lambda: self.heard)
        self.assertEqual(self.heard[0].line, "Tabletop couldn't complete that. No tables left.")

    async def test_user_limit(self):
        for _ in range(8):
            await self.dispatch(intent="never finish", slots={})
        agent = self.router.get(self.agent_row["id"])
        out = await self.svc.dispatch(user_id=self.user, agent=agent, intent="one more")
        self.assertEqual((out.ok, out.reason), (False, "user_limit"))

    async def test_lost_ack_is_resent_with_same_task_and_runs_once(self):
        self.agent._drop_acks = 1
        task = await self.dispatch(intent="slow job", slots={})
        tid = task["task_id"]
        await wait_for(lambda: self.agent.dispatch_count.get(tid, 0) >= 2, timeout=10)
        await wait_for(self._running(tid), timeout=10)
        self.assertEqual(len([t for t in self.agent.tasks if t == tid]), 1)

    def _running(self, tid):
        async def check():
            return await self.status(tid) in ("running", "completed")
        return check


class FailoverTests(ServiceTestBase):
    async def test_unnamed_request_fails_over_from_unreachable_agent(self):
        dead = await asyncio.to_thread(lambda: agents_registry.create_agent(
            name="DeadTable", url=f"ws://127.0.0.1:{free_port()}/relay", service_id="dead",
            extra={"modes": ["task"], "events": ["callback"], "max_reply_latency_s": 1},
        ))
        self.router.load_now()
        self.listen()
        out = await self.svc.dispatch(
            user_id=self.user, agent=self.router.get(dead["id"]), intent="book a dinner",
            chain=[self.router.get(self.agent_row["id"])], named=False,
        )
        tid = out.task["task_id"]
        await wait_for(lambda: self.heard, timeout=15)
        self.assertEqual((await self.svc.get(tid))["agent_id"], self.agent_row["id"])
        self.assertIn("Tabletop finished", self.heard[0].line)

    async def test_named_unreachable_agent_is_reported_not_substituted(self):
        dead = await asyncio.to_thread(lambda: agents_registry.create_agent(
            name="DeadTable", url=f"ws://127.0.0.1:{free_port()}/relay", service_id="dead",
            extra={"modes": ["task"], "events": ["callback"], "max_reply_latency_s": 1},
        ))
        self.router.load_now()
        self.listen()
        out = await self.svc.dispatch(
            user_id=self.user, agent=self.router.get(dead["id"]), intent="book a dinner", named=True,
            chain=[self.router.get(self.agent_row["id"])],
        )
        await wait_for(lambda: self.heard, timeout=15)
        self.assertEqual(self.heard[0].line, "DeadTable isn't answering right now.")
        self.assertEqual(await self.status(out.task["task_id"]), "failed")
        self.assertEqual(self.agent.tasks, {})


class CrudTests(ServiceTestBase):
    async def test_cancel_update_complete_delete(self):
        t1 = await self.dispatch(intent="never finish", slots={"time": "19:00"})
        await wait_for(self._running(t1["task_id"]))
        line = await self.svc.update(await self.svc.get(t1["task_id"]), {"time": "20:00"})
        self.assertEqual(line, "Okay, I've told Tabletop about the change.")
        await wait_for(lambda: self.agent.tasks[t1["task_id"]].updates == [{"time": "20:00"}])
        line = await self.svc.cancel(await self.svc.get(t1["task_id"]))
        self.assertTrue(line.startswith("Okay, I've cancelled your request to never finish"))
        await wait_for(lambda: self.agent.tasks[t1["task_id"]].cancelled)

        t2 = await self.dispatch(intent="never finish two", slots={})
        await wait_for(self._running(t2["task_id"]))
        await self.svc.complete(await self.svc.get(t2["task_id"]))
        await wait_for(lambda: self.agent.tasks[t2["task_id"]].closed_reason == "completed_by_user")
        self.assertEqual(await self.status(t2["task_id"]), "completed")

        await self.svc.delete(await self.svc.get(t2["task_id"]))
        await wait_for(self._gone(t2["task_id"]))
        self.assertEqual(self.agent.tasks[t2["task_id"]].closed_reason, "deleted")

    def _running(self, tid):
        async def check():
            return await self.status(tid) == "running"
        return check

    def _gone(self, tid):
        async def check():
            return await self.svc.get(tid) is None
        return check

    async def test_update_without_update_op_recreates_task(self):
        self.agent.task_ops = ("dispatch", "cancel", "input")
        await asyncio.to_thread(lambda: agents_registry.update_agent(self.agent_row["id"], {"task_ops": ["dispatch", "cancel", "input"]}))
        self.router.load_now()
        t = await self.dispatch(intent="never finish", slots={"time": "19:00"})
        await wait_for(self._running(t["task_id"]))
        line = await self.svc.update(await self.svc.get(t["task_id"]), {"time": "20:00"})
        self.assertEqual(line, "Okay, I've told Tabletop about the change.")
        self.assertEqual(await self.status(t["task_id"]), "cancelled")
        tasks = await self.svc.list(self.user, active_only=True)
        self.assertEqual(tasks[0]["task_info"]["slots"]["time"], "20:00")


class DeliveryTests(ServiceTestBase):
    async def test_next_session_announced_at_session_start(self):
        task = await self.dispatch()
        await wait_for(self._done(task["task_id"]))
        items = await self.svc.session_start(self.user)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0][1].startswith("Tabletop finished"))
        await wait_for(lambda: self.agent.tasks[task["task_id"]].delivered_via == "next_session")
        self.assertEqual(await self.svc.session_start(self.user), [])

    def _done(self, tid):
        async def check():
            return await self.status(tid) == "completed"
        return check

    async def test_session_start_caps_at_three(self):
        for i in range(5):
            t = await self.dispatch(intent=f"job {i}", slots={})
            await wait_for(self._done(t["task_id"]))
        items = await self.svc.session_start(self.user)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[-1][1], "You have 2 more task updates. Want to hear them?")

    async def test_device_wake_and_limits(self):
        t = await self.dispatch(notify="device")
        # The status flips to completed just before the wake job is queued.
        await wait_for(lambda: pgfix.query("SELECT 1 FROM jobs WHERE kind = 'task_result'"))
        jobs = pgfix.query("SELECT kind, payload->>'task_id' FROM jobs WHERE kind = 'task_result'")
        self.assertEqual(jobs, [("task_result", t["task_id"])])
        # A second result while a wake is pending shares it.
        t2 = await self.dispatch(notify="device", intent="another")
        await wait_for(self._done(t2["task_id"]))
        await asyncio.sleep(0.3)
        self.assertEqual(len(pgfix.query("SELECT 1 FROM jobs WHERE kind = 'task_result'")), 1)
        # Hourly cap.
        pgfix.query("UPDATE jobs SET done_at = now()")
        pgfix.query("INSERT INTO jobs (kind, payload) SELECT 'task_result', jsonb_build_object('user_id', %s::text) "
                    "FROM generate_series(1, 4)", (self.user,))
        pgfix.query("UPDATE jobs SET done_at = now()")
        t3 = await self.dispatch(notify="device", intent="third")
        await wait_for(self._done(t3["task_id"]))
        await asyncio.sleep(0.3)
        self.assertEqual(len(pgfix.query("SELECT 1 FROM jobs WHERE kind = 'task_result' AND done_at IS NULL")), 0)

    async def test_quiet_hours_suppress_wake(self):
        os.environ["ORCHESTRATOR_QUIET_HOURS"] = "0-24"
        try:
            t = await self.dispatch(notify="device")
            await wait_for(self._done(t["task_id"]))
            await asyncio.sleep(0.2)
            self.assertEqual(pgfix.query("SELECT 1 FROM jobs WHERE kind = 'task_result'"), [])
        finally:
            os.environ["ORCHESTRATOR_QUIET_HOURS"] = "0-0"

    async def test_silent_is_recorded_only(self):
        t = await self.dispatch(notify="silent")
        await wait_for(self._done(t["task_id"]))
        self.assertEqual(await self.svc.session_start(self.user), [])
        self.assertEqual(pgfix.query("SELECT 1 FROM jobs WHERE kind = 'task_result'"), [])

    async def test_held_question_announced_next_session(self):
        t = await self.dispatch(intent="confirm a booking")
        await wait_for(lambda: self._status_is(t["task_id"], "input_required"))
        items = await self.svc.session_start(self.user)
        self.assertEqual(items[0][1], "Tabletop needs something from you: Which restaurant?")

    async def _status_is(self, tid, st):
        return await self.status(tid) == st


class DeadlineTests(ServiceTestBase):
    async def test_deadline_scheduled_after_ack_and_cancelled_on_finish(self):
        t = await self.dispatch(deadline_at=utcnow() + timedelta(hours=1))
        await wait_for(self._done(t["task_id"]))
        self.assertEqual(pgfix.query("SELECT 1 FROM jobs WHERE kind = 'agent_task_deadline'"), [])

    def _done(self, tid):
        async def check():
            return await self.status(tid) == "completed"
        return check

    async def test_drop_at_deadline_times_out_and_cancels(self):
        self.listen()
        t = await self.dispatch(intent="never finish", slots={}, deadline_at=utcnow() + timedelta(hours=1),
                                drop_at_deadline=True)
        tid = t["task_id"]
        await wait_for(lambda: pgfix.query("SELECT payload FROM jobs WHERE kind = 'agent_task_deadline'"))
        payload = pgfix.query("SELECT payload FROM jobs WHERE kind = 'agent_task_deadline'")[0][0]
        self.assertEqual((payload["task_id"], payload["on_deadline"]), (tid, "cancel"))
        self.assertEqual(await self.svc.on_deadline(tid, payload), "timed_out")
        await wait_for(lambda: self.agent.tasks[tid].cancelled)
        self.assertEqual(await self.status(tid), "timed_out")
        self.assertIn("so I cancelled it", self.heard[-1].line)

    async def test_default_notify_overdue_then_late_result_wins(self):
        self.listen()
        t = await self.dispatch(intent="never finish", slots={}, deadline_at=utcnow() + timedelta(hours=1))
        tid = t["task_id"]
        await wait_for(lambda: pgfix.query("SELECT payload FROM jobs WHERE kind = 'agent_task_deadline'"))
        payload = pgfix.query("SELECT payload FROM jobs WHERE kind = 'agent_task_deadline'")[0][0]
        self.assertEqual(await self.svc.on_deadline(tid, payload), "overdue")
        self.assertIn("still hasn't finished", self.heard[-1].line)
        self.assertEqual(await self.status(tid), "running")
        await self.agent.emit(self.agent.tasks[tid], {"status": proto.COMPLETED, "result": {"say": "Booked."}})
        await wait_for(lambda: self.heard[-1].line == "Tabletop finished: Booked.")

    async def test_stale_deadline_job_is_ignored(self):
        t = await self.dispatch(intent="never finish", slots={}, deadline_at=utcnow() + timedelta(hours=1))
        await wait_for(lambda: pgfix.query("SELECT 1 FROM jobs WHERE kind = 'agent_task_deadline'"))
        later = utcnow() + timedelta(hours=2)
        await self.svc.set_deadline(await self.svc.get(t["task_id"]), later)
        old = {"deadline_at": (utcnow() + timedelta(hours=1)).isoformat()}
        self.assertEqual(await self.svc.on_deadline(t["task_id"], old), "stale")
        rows = pgfix.query("SELECT 1 FROM jobs WHERE kind = 'agent_task_deadline' AND done_at IS NULL")
        self.assertEqual(len(rows), 1)


class LivenessAndHttpTests(ServiceTestBase):
    async def test_heartbeat_reconciliation_marks_lost_tasks(self):
        self.listen()
        t = await self.dispatch(intent="never finish", slots={})
        await wait_for(lambda: self._status_is(t["task_id"], "running"))
        self.agent.tasks.clear()  # the agent restarted and lost everything
        resp = await self.agent.register(self.base, f"ws://127.0.0.1:{self.agent.port}/relay")
        self.assertEqual((resp["ok"], resp["lost_tasks"]), (True, 1))
        self.assertEqual(await self.status(t["task_id"]), "failed")
        self.assertEqual(self.heard[-1].line, "Tabletop lost track of your request to never finish.")

    async def _status_is(self, tid, st):
        return await self.status(tid) == st

    async def test_stall_flag(self):
        t = await self.dispatch(intent="never finish", slots={})
        await wait_for(lambda: self._status_is(t["task_id"], "running"))
        old = (utcnow() - timedelta(hours=1)).isoformat()
        pgfix.query("UPDATE agents SET agent_info = agent_info || jsonb_build_object('last_seen', %s::text)", (old,))
        self.assertEqual(await self.svc.stall_check(), 1)
        line = self.svc.status_line(await self.svc.get(t["task_id"]))
        self.assertEqual(line, "Tabletop hasn't checked in for a while, so your request to never finish may be stuck.")
        self.assertEqual(await self.svc.stall_check(), 0)  # announced once

    async def test_callback_auth_and_dedupe(self):
        t = await self.dispatch(intent="never finish", slots={})
        tid = t["task_id"]
        await wait_for(lambda: tid in self.agent.tasks)
        url = f"{self.base}/developer/tasks/{tid}/events"
        ev = proto.envelope(proto.TASK_EVENT, task_id=tid, seq=1, body={"status": "working"})
        async with httpx.AsyncClient() as c:
            self.assertEqual((await c.post(url, json=ev)).status_code, 401)
            self.assertEqual((await c.post(url, json=ev, headers={"Authorization": "Bearer nope"})).status_code, 401)
            r = await c.post(f"{self.base}/developer/tasks/00000000-0000-0000-0000-000000000000/events", json=ev,
                             headers={"Authorization": "Bearer x"})
            self.assertEqual(r.status_code, 404)
            token = self.agent.tasks[tid].callback["token"]
            await wait_for(lambda: self.agent.tasks[tid].acked_seq >= 1)
            r = await c.post(url, json=ev, headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(r.json()["ack_seq"], self.agent.tasks[tid].seq)  # duplicate seq: ack, no re-apply

    async def test_dispatch_api_fails_closed_and_requires_token(self):
        payload = {"user_id": self.user, "intent": "book nopa", "agent": "Tabletop"}
        os.environ.pop("DISPATCH_API_TOKEN", None)
        async with httpx.AsyncClient() as c:
            self.assertEqual((await c.post(f"{self.base}/api/dispatch", json=payload)).status_code, 503)
            os.environ["DISPATCH_API_TOKEN"] = "secret"
            try:
                self.assertEqual((await c.post(f"{self.base}/api/dispatch", json=payload,
                                               headers={"Authorization": "Bearer bad"})).status_code, 401)
                r = await c.post(f"{self.base}/api/dispatch", json=payload, headers={"Authorization": "Bearer secret"})
                self.assertEqual(r.status_code, 202, r.text)
                tid = r.json()["task"]["task_id"]
                r = await c.get(f"{self.base}/api/dispatch/{tid}", headers={"Authorization": "Bearer secret"})
                self.assertEqual(r.json()["task_id"], tid)
                r = await c.post(f"{self.base}/api/dispatch", json={**payload, "agent": "Zorblax"},
                                 headers={"Authorization": "Bearer secret"})
                self.assertEqual(r.status_code, 404)
            finally:
                os.environ.pop("DISPATCH_API_TOKEN", None)

    async def test_internal_deadline_hook_requires_token(self):
        async with httpx.AsyncClient() as c:
            # No INTERNAL_API_TOKEN and no DB_PASSWORD to derive one from: fail closed.
            r = await c.post(f"{self.base}/internal/tasks/x/deadline", json={})
            self.assertEqual(r.status_code, 503)
            os.environ["INTERNAL_API_TOKEN"] = "itok"
            self.addCleanup(os.environ.pop, "INTERNAL_API_TOKEN", None)
            r = await c.post(f"{self.base}/internal/tasks/x/deadline", json={})
            self.assertEqual(r.status_code, 401)
            r = await c.post(f"{self.base}/internal/tasks/00000000-0000-0000-0000-000000000000/deadline", json={},
                             headers={"Authorization": f"Bearer {orch_routes.internal_token()}"})
            self.assertEqual(r.json()["outcome"], "stale")

    async def test_registration_v2_fields_and_heartbeat_preserves_website_edits(self):
        resp = await self.agent.register(self.base, f"ws://127.0.0.1:{self.agent.port}/relay", name="Tabletop",
                                         side_effects=False, domains=["dining"])
        self.assertTrue(resp["ok"])
        agent = await asyncio.to_thread(agents_registry.get_agent, self.agent_row["id"])
        self.assertEqual(agent["domains"], ["dining"])
        self.assertIsNotNone(agent["last_seen"])
        await asyncio.to_thread(lambda: agents_registry.update_agent(self.agent_row["id"], {"side_effects": True}))
        await self.agent.register(self.base, f"ws://127.0.0.1:{self.agent.port}/relay", side_effects=False)
        agent = await asyncio.to_thread(agents_registry.get_agent, self.agent_row["id"])
        self.assertTrue(agent["side_effects"])  # owner's website edit wins over self-reporting

    async def test_first_party_backfill(self):
        k = await asyncio.to_thread(lambda: agents_registry.create_agent(name="Kairos", url="wss://x/ws/{user_id}"))
        self.assertEqual(k["routing_policy"], "owns_domain")
        self.assertIn("my list", k["intent_aliases"])
        m = await self.agent.register(self.base, "ws://127.0.0.1:1/relay", name="MyFitnessPal") if False else None
        reg = await asyncio.to_thread(lambda: agents_registry.upsert_registration(
            service_id="mfp", public_url="ws://mfp/relay", name="MyFitnessPal"))
        self.assertEqual(reg["routing_policy"], "on_request")
        self.assertIn("calories", reg["intent_aliases"])
        self.assertIsNone(m)


class AuditFixTests(ServiceTestBase):
    def _done(self, tid, st="completed"):
        async def check():
            return await self.status(tid) == st
        return check

    async def test_silent_result_not_spoken_even_with_live_session(self):
        self.listen()
        t = await self.dispatch(notify="silent")
        await wait_for(self._done(t["task_id"]))
        await asyncio.sleep(0.2)
        self.assertEqual(self.heard, [])

    async def test_silent_task_question_is_still_relayed(self):
        self.listen()
        await self.dispatch(intent="confirm a booking", notify="silent")
        await wait_for(lambda: self.heard)
        self.assertEqual(self.heard[0].kind, "question")

    async def test_update_unsupported_nack_recreates(self):
        self.listen()
        t = await self.dispatch(intent="never finish frozen", slots={"time": "19:00"})
        await wait_for(self._done(t["task_id"], "running"))
        await self.svc.update(await self.svc.get(t["task_id"]), {"time": "20:00"})
        await wait_for(lambda: self.heard)
        self.assertEqual(self.heard[0].line, "Okay, I've told Tabletop about the change.")
        self.assertEqual(await self.status(t["task_id"]), "cancelled")
        active = await self.svc.list(self.user, active_only=True)
        self.assertEqual(active[0]["task_info"]["slots"]["time"], "20:00")

    async def test_cannot_meet_deadline_offers_retry(self):
        self.listen()
        t = await self.dispatch(intent="tight booking", slots={}, deadline_at=utcnow() + timedelta(minutes=5))
        await wait_for(lambda: self.heard)
        ann = self.heard[0]
        self.assertEqual(ann.offer, "retry_without_deadline")
        self.assertEqual(ann.line, "Tabletop can't finish that by the deadline. It takes at least a day. "
                                   "Want me to try without a deadline?")
        await self.svc.retry_without_deadline(await self.svc.get(t["task_id"]))
        await wait_for(lambda: len(self.heard) >= 2)
        self.assertEqual(await self.status(t["task_id"]), "completed")

    async def test_overdue_offers_cancel(self):
        self.listen()
        t = await self.dispatch(intent="never finish", slots={}, deadline_at=utcnow() + timedelta(hours=1))
        await wait_for(lambda: pgfix.query("SELECT 1 FROM jobs WHERE kind = 'agent_task_deadline'"))
        payload = pgfix.query("SELECT payload FROM jobs WHERE kind = 'agent_task_deadline'")[0][0]
        await self.svc.on_deadline(t["task_id"], payload)
        self.assertEqual(self.heard[-1].offer, "cancel")

    async def test_event_without_seq_is_rejected(self):
        t = await self.dispatch(intent="never finish", slots={})
        await wait_for(lambda: t["task_id"] in self.agent.tasks)
        token = self.agent.tasks[t["task_id"]].callback["token"]
        ev = proto.envelope(proto.TASK_EVENT, task_id=t["task_id"], body={"status": "completed"})
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{self.base}/developer/tasks/{t['task_id']}/events", json=ev,
                             headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 400)

    async def test_delete_without_close_op_still_deletes(self):
        await asyncio.to_thread(lambda: agents_registry.update_agent(
            self.agent_row["id"], {"task_ops": ["dispatch", "cancel", "input"]}))
        self.router.load_now()
        t = await self.dispatch(intent="never finish", slots={})
        await wait_for(self._done(t["task_id"], "running"))
        await self.svc.delete(await self.svc.get(t["task_id"]))

        async def gone():
            return await self.svc.get(t["task_id"]) is None
        await wait_for(gone)

    async def test_adopt_respects_user_limit(self):
        for _ in range(8):
            await self.dispatch(intent="never finish", slots={})
        with self.assertRaises(service_mod.TaskError):
            await self.svc.adopt(self.router.get(self.agent_row["id"]), self.user, None, {"intent": "more"})

    async def test_probe_unknown_task_marks_lost(self):
        self.listen()
        t = await self.dispatch(intent="never finish", slots={})
        await wait_for(self._done(t["task_id"], "running"))
        self.agent.tasks.clear()
        self.assertTrue(await self.svc.probe(await self.svc.get(t["task_id"])))
        await wait_for(self._done(t["task_id"], "failed"))
        self.assertIn("lost track", self.heard[-1].line)

    async def test_deadline_sent_as_utc_z(self):
        t = await self.dispatch(intent="never finish", slots={}, deadline_at=utcnow() + timedelta(hours=1))
        await wait_for(lambda: t["task_id"] in self.agent.tasks)
        dispatch = next(m for m in self.agent.received if m.get("type") == "task.dispatch")
        self.assertTrue(dispatch["body"]["deadline_at"].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
