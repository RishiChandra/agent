"""Kairos task mode (app/kairos_tasks.py) against the real TaskService.

The orchestrator dials Kairos's registered URL (`/ws/{user_id}`, filled in as
`orchestrator`), dispatches, and gets the result back on the callback. Kairos's
text agent is replaced by a stub so no model is called.

    python -m pytest test/app/orchestrator/test_kairos_tasks.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import pgfix  # noqa: E402,F401  (sets sys.path to app/)
assert pgfix
from test_service import ServiceTestBase, wait_for  # noqa: E402

from fastapi import WebSocket  # noqa: E402

import agents_registry  # noqa: E402
import kairos_tasks  # noqa: E402
from orchestrator.tasks import protocol as proto  # noqa: E402


class KairosTaskTests(ServiceTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        os.environ["KAIROS_CALLBACK_BASE"] = self.base
        kairos_tasks._agent = kairos_tasks.KairosTaskAgent()
        self.thought: list[tuple[str, str]] = []
        self.release = threading.Event()
        self.release.set()
        self.think_error: Exception | None = None

        def fake_think(user_id, request):
            self.thought.append((user_id, request))
            self.release.wait(10)
            if self.think_error:
                raise self.think_error
            return "Okay, I set a reminder to go to the gym tomorrow at 10 AM."

        self._real_think = kairos_tasks._think
        kairos_tasks._think = fake_think

        async def ws_user(websocket: WebSocket, user_id: str):
            await websocket.accept()
            if user_id == proto.TASK_USER_ID:
                await kairos_tasks.task_endpoint(websocket)
            else:
                await websocket.close()

        self.server.config.app.websocket("/ws/{user_id}")(ws_user)
        row = await asyncio.to_thread(lambda: agents_registry.create_agent(
            name="Kairos", url=f"ws://127.0.0.1:{self.port}/ws/{{user_id}}",
            description="Tasks and reminders", service_id="kairos",
        ))
        await asyncio.to_thread(agents_registry.backfill_routing_and_embeddings)
        self.router.load_now()
        self.kairos = self.router.get(row["id"])

    async def asyncTearDown(self):
        self.release.set()
        kairos_tasks._think = self._real_think
        os.environ.pop("KAIROS_CALLBACK_BASE", None)
        await super().asyncTearDown()

    async def kdispatch(self, intent="set a reminder to go to the gym at ten am tomorrow", **kw):
        out = await self.svc.dispatch(user_id=self.user, agent=self.kairos, intent=intent, **kw)
        self.assertTrue(out.ok, out.reason)
        return out.task["task_id"]

    async def test_backfill_gives_kairos_task_mode(self):
        self.assertIn(proto.MODE_TASK, self.kairos.modes)
        self.assertIn(proto.MODE_BRIDGE, self.kairos.modes)
        self.assertTrue(self.kairos.supports(proto.MODE_TASK))

    async def test_dispatch_runs_text_agent_and_reports_result(self):
        self.listen()
        tid = await self.kdispatch(slots={"time": "10 am tomorrow"})
        await wait_for(lambda: self.heard)
        self.assertEqual(await self.status(tid), "completed")
        self.assertEqual(self.thought, [(self.user, "set a reminder to go to the gym at ten am tomorrow (time: 10 am tomorrow)")])
        self.assertIn("gym tomorrow at 10 AM", self.heard[0].line)

    async def test_failure_is_reported(self):
        self.think_error = RuntimeError("model down")
        self.listen()
        tid = await self.kdispatch()
        await wait_for(self._status_is(tid, "failed"))

    async def test_cancel_while_thinking_drops_result(self):
        self.release.clear()
        self.listen()
        tid = await self.kdispatch()
        await wait_for(lambda: self.thought)
        await wait_for(lambda: tid in kairos_tasks.get_agent().tasks)
        await self.svc.cancel(await self.svc.get(tid))
        await wait_for(self._status_is(tid, "cancelled"))
        ktask = kairos_tasks.get_agent().tasks[tid]
        await wait_for(lambda: ktask.cancelled)  # Kairos got task.cancel
        self.release.set()
        await asyncio.sleep(0.5)
        self.assertEqual(await self.status(tid), "cancelled")

    def _status_is(self, tid, want):
        async def check():
            return await self.status(tid) == want
        return check

    async def test_status_of_unknown_task_nacks(self):
        reply = kairos_tasks.get_agent().handle(proto.envelope(proto.TASK_STATUS, task_id="nope"))
        self.assertEqual(reply["type"], proto.TASK_NACK)
        self.assertEqual(reply["body"]["code"], proto.NACK_UNKNOWN_TASK)

    async def test_callback_to_foreign_host_is_refused(self):
        msg = proto.envelope(proto.TASK_DISPATCH, task_id="t1", body={
            "user_id": self.user, "intent": "remind me",
            "callback": {"url": "http://evil.example/steal", "token": "x"},
        })
        reply = kairos_tasks.get_agent().handle(msg)
        self.assertEqual(reply["type"], proto.TASK_NACK)
        self.assertEqual(reply["body"]["code"], proto.NACK_INVALID_INPUT)

    def test_task_url_fills_user_id(self):
        self.assertEqual(proto.task_url("wss://h/ws/{user_id}"), "wss://h/ws/orchestrator")
        self.assertEqual(proto.task_url("wss://h/relay"), "wss://h/relay")
