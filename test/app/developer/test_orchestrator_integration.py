"""End-to-end tests for orchestrator v2: HTTP routes + voice tool handlers + the
reference echo agent, all in one event loop.

What runs for real: the FastAPI app (`app/main.py`, lifespan skipped), the echo
agent (`developer_ws/testing/echo_server.py`) under uvicorn on a free port, the
router, the dispatcher, and `SpeechPipeline`'s tool handlers. What's faked: the
database (router fed synthetic rows) and the audio side of `SpeechPipeline`
(frames are captured instead of synthesised).

Needs the app's Python deps (fastapi, pipecat-ai, httpx, uvicorn, websockets);
skipped if they aren't installed. Run from repo root:

    python -m pytest test/app/developer/test_orchestrator_integration.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, os.path.join(project_root, "app"))
os.environ.setdefault("GOOGLE_API_KEY", "test-dummy")  # gemini_config reads it at import

_HAVE_DEPS = all(
    importlib.util.find_spec(m) is not None
    for m in ("fastapi", "pipecat", "httpx", "uvicorn", "websockets")
)

if _HAVE_DEPS:
    import httpx
    import uvicorn
    from pipecat.frames.frames import TTSSpeakFrame
    from starlette.websockets import WebSocketState

    import agent_router as ar
    import task_dispatcher as td
    from developer_ws.bridge import BridgeStartResult
    from developer_ws.pipeline import SpeechPipeline
    from developer_ws.testing import echo_server


class _FakeLLM:
    def __init__(self, sink: list):
        self.sink = sink
        self.announcements: list[str] = []

    def add_assistant_announcement(self, text: str) -> None:
        self.announcements.append(text)

    async def push_frame(self, frame, *_a, **_k) -> None:
        self.sink.append(frame)


class _FakeTask:
    def __init__(self, sink: list):
        self.sink = sink

    async def queue_frame(self, frame) -> None:
        self.sink.append(frame)


class _FakeBridge:
    def __init__(self):
        self.active = False
        self.started: list[str] = []

    async def start(self, url: str):
        self.started.append(url)
        return BridgeStartResult(ok=True, outcome="ok", service_id="x")


class _Params:
    def __init__(self, llm, arguments: dict):
        self.llm = llm
        self.arguments = arguments
        self.result = None

    async def result_callback(self, result, properties=None) -> None:
        self.result = result


@unittest.skipUnless(_HAVE_DEPS, "app dependencies (fastapi, pipecat, ...) not installed")
class OrchestratorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Real echo agent on a free port.
        config = uvicorn.Config(echo_server.app, host="127.0.0.1", port=0, log_level="warning", ws="websockets")
        self.echo = uvicorn.Server(config)
        self.echo_task = asyncio.create_task(self.echo.serve())
        for _ in range(200):
            if self.echo.started:
                break
            await asyncio.sleep(0.02)
        port = self.echo.servers[0].sockets[0].getsockname()[1]
        echo_url = f"ws://127.0.0.1:{port}/relay"

        rows = [
            {"id": "echo", "name": "Echo", "url": echo_url, "description": "Echoes requests back",
             "modes": ["bridge", "task"], "keywords": ["echo", "test"]},
            {"id": "kairos", "name": "Kairos", "url": echo_url, "description": "Calendar and scheduling",
             "modes": ["bridge"], "keywords": ["calendar"]},
            {"id": "wb1", "name": "Weather Bot", "url": echo_url, "description": "Weather", "modes": ["bridge", "task"]},
            {"id": "wb2", "name": "Weather Bat", "url": echo_url, "description": "Weather", "modes": ["bridge", "task"]},
        ]
        rows += [
            {"id": f"f{i}", "name": f"Filler Agent {i}", "url": echo_url, "description": f"filler {i}"}
            for i in range(1200)
        ]
        self.router = ar.AgentRouter(loader=lambda: rows)
        self.router.load_now()
        ar.set_router(self.router)
        self.dispatcher = td.TaskDispatcher(self.router)
        td.set_dispatcher(self.dispatcher)

        import main  # imported late so the env default above applies

        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.dispatcher.shutdown()
        ar.set_router(None)
        td.set_dispatcher(None)
        os.environ.pop("DISPATCH_API_TOKEN", None)
        self.echo.should_exit = True
        await asyncio.wait_for(self.echo_task, timeout=5)

    # ----- helpers -------------------------------------------------------------

    def make_pipeline(self, user_id: str = "u1"):
        spoken: list = []
        pipe = SpeechPipeline.__new__(SpeechPipeline)
        pipe._user_id = user_id
        pipe._llm = _FakeLLM(spoken)
        pipe._task = _FakeTask(spoken)
        pipe._bridge = _FakeBridge()
        pipe._ws = SimpleNamespace(client_state=WebSocketState.CONNECTED)
        pipe._dispatcher = self.dispatcher
        pipe._held_task_announcements = []
        pipe._unsubscribe_tasks = self.dispatcher.subscribe(user_id, pipe._on_task_update)
        return pipe, spoken

    @staticmethod
    def texts(frames) -> list[str]:
        return [f.text for f in frames if isinstance(f, TTSSpeakFrame)]

    async def wait_for(self, pred, timeout=5.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if pred():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("condition not met in time")

    # ----- HTTP -----------------------------------------------------------------

    async def test_search_endpoint_handles_stt_garble(self):
        r = await self.http.get("/api/agents/search", params={"q": "cairo's"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["results"][0]["name"], "Kairos")

    async def test_http_dispatch_round_trip(self):
        r = await self.http.post("/api/dispatch", json={"user_id": "api-user", "agent": "echo", "intent": "ping the lab"})
        self.assertEqual(r.status_code, 202, r.text)
        tid = r.json()["task"]["task_id"]

        async def status():
            return (await self.http.get(f"/api/dispatch/{tid}")).json()["status"]

        for _ in range(250):
            if await status() in td.TERMINAL:
                break
            await asyncio.sleep(0.02)
        body = (await self.http.get(f"/api/dispatch/{tid}")).json()
        self.assertEqual(body["status"], "succeeded", body)
        self.assertIn("ping the lab", body["say"])
        self.assertEqual(body["output"]["echo"], "ping the lab")
        listed = (await self.http.get("/api/dispatch/user/api-user")).json()["tasks"]
        self.assertEqual(listed[0]["task_id"], tid)

    async def test_http_dispatch_errors(self):
        r = await self.http.post("/api/dispatch", json={"user_id": "u", "agent": "xyzzy plugh", "intent": "x"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"]["reason"], "no_agent")
        r = await self.http.post("/api/dispatch", json={"user_id": "u", "agent": "kairos", "intent": "x"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["reason"], "wrong_mode")
        r = await self.http.get("/api/dispatch/task_nope")
        self.assertEqual(r.status_code, 404)

    async def test_http_dispatch_token(self):
        os.environ["DISPATCH_API_TOKEN"] = "s3cret"
        body = {"user_id": "u", "agent": "echo", "intent": "x"}
        self.assertEqual((await self.http.post("/api/dispatch", json=body)).status_code, 401)
        r = await self.http.post("/api/dispatch", json=body, headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(r.status_code, 202)

    async def test_router_stats(self):
        r = await self.http.get("/api/router/stats")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["router"]["agents"], 1204)

    # ----- voice tool handlers -------------------------------------------------

    async def test_voice_dispatch_acks_then_announces_result(self):
        pipe, spoken = self.make_pipeline()
        params = _Params(pipe._llm, {"intent": "water the plants", "agent": "echo"})
        await pipe._handle_dispatch_task(params)
        self.assertTrue(params.result["ok"])
        self.assertEqual(self.texts(spoken)[0], "Okay, I've asked Echo to handle that. I'll let you know when it's done.")
        await self.wait_for(lambda: len(self.texts(spoken)) >= 2)
        self.assertIn("water the plants", self.texts(spoken)[1])
        # The result is in the LLM's history, so follow-ups have context.
        self.assertIn(self.texts(spoken)[1], pipe._llm.announcements)

    async def test_voice_dispatch_input_required_round_trip(self):
        pipe, spoken = self.make_pipeline()
        await pipe._handle_dispatch_task(_Params(pipe._llm, {"intent": "confirm the order", "agent": "echo"}))
        await self.wait_for(lambda: any("needs something from you" in t for t in self.texts(spoken)))
        params = _Params(pipe._llm, {"answer": "yes please"})
        await pipe._handle_answer_agent(params)
        self.assertTrue(params.result["ok"])
        await self.wait_for(lambda: any("Done: confirm the order" in t for t in self.texts(spoken)))

    async def test_voice_dispatch_ambiguous_asks(self):
        pipe, spoken = self.make_pipeline()
        params = _Params(pipe._llm, {"intent": "forecast", "agent": "weather b"})
        await pipe._handle_dispatch_task(params)
        self.assertFalse(params.result["ok"])
        self.assertTrue(self.texts(spoken)[0].startswith("Did you mean"))
        self.assertIn("Weather Bot", self.texts(spoken)[0])

    async def test_voice_bridge_resolves_garbled_name(self):
        pipe, spoken = self.make_pipeline()
        params = _Params(pipe._llm, {"reason": "check calendar", "agent": "kai ross"})
        await pipe._handle_start_remote_audio_bridge(params)
        self.assertTrue(params.result["ok"])
        self.assertEqual(len(pipe._bridge.started), 1)
        self.assertEqual(self.texts(spoken)[0], "Connecting you to Kairos now.")

    async def test_voice_bridge_unknown_name_does_not_dial(self):
        pipe, spoken = self.make_pipeline()
        params = _Params(pipe._llm, {"reason": "x", "agent": "xyzzy plugh"})
        await pipe._handle_start_remote_audio_bridge(params)
        self.assertEqual(pipe._bridge.started, [])
        self.assertEqual(self.texts(spoken)[0], "I couldn't find an agent called xyzzy plugh.")

    async def test_voice_bridge_ambiguous_does_not_dial(self):
        pipe, spoken = self.make_pipeline()
        params = _Params(pipe._llm, {"reason": "x", "agent": "weather b"})
        await pipe._handle_start_remote_audio_bridge(params)
        self.assertEqual(pipe._bridge.started, [])
        self.assertTrue(self.texts(spoken)[0].startswith("Did you mean"))

    async def test_voice_find_agents(self):
        pipe, spoken = self.make_pipeline()
        params = _Params(pipe._llm, {"query": "calendar scheduling"})
        await pipe._handle_find_agents(params)
        self.assertIn("Kairos", self.texts(spoken)[0])

    async def test_voice_cancel_and_status(self):
        pipe, spoken = self.make_pipeline()
        os.environ["ECHO_TASK_WORK_S"] = "5"
        echo_server.TASK_WORK_S = 5.0
        try:
            await pipe._handle_dispatch_task(_Params(pipe._llm, {"intent": "long job", "agent": "echo"}))
            await asyncio.sleep(0.2)
            await pipe._handle_check_tasks(_Params(pipe._llm, {}))
            self.assertIn("Echo is working on it", self.texts(spoken)[-1])
            p = _Params(pipe._llm, {})
            await pipe._handle_cancel_task(p)
            self.assertTrue(p.result["ok"])
            self.assertEqual(self.texts(spoken)[-1], "Okay, I cancelled the task with Echo.")
        finally:
            echo_server.TASK_WORK_S = 0.2
            os.environ.pop("ECHO_TASK_WORK_S", None)

    async def test_results_held_while_bridged(self):
        pipe, spoken = self.make_pipeline()
        pipe._bridge.active = True
        await pipe._handle_dispatch_task(_Params(pipe._llm, {"intent": "quiet job", "agent": "echo"}))
        await self.wait_for(lambda: pipe._held_task_announcements)
        self.assertFalse(any("quiet job" in t for t in self.texts(spoken)))
        pipe._bridge.active = False
        await pipe._flush_held_task_announcements()
        self.assertTrue(any("quiet job" in t for t in self.texts(spoken)))

    async def test_bridge_v2_hello_against_echo_and_v1_agent(self):
        """The real RemoteAudioBridge speaks v2 and still works with a v1-only agent."""
        import json as _json

        import websockets
        from developer_ws.bridge import RemoteAudioBridge

        audio = SimpleNamespace(add_playback_pcm=lambda pcm: None, mark_turn_complete=lambda: None)

        bridge = RemoteAudioBridge(audio, user_id="u1")
        res = await bridge.start(self.router.get("echo").url)
        self.assertTrue(res.ok, res)
        await bridge.close()

        hellos = []

        async def v1_agent(ws):
            hellos.append(_json.loads(await ws.recv()))
            # A v1 agent: no modes, version "1".
            await ws.send(_json.dumps({"type": "ack", "accept": True, "service_id": "old", "version": "1"}))
            try:
                async for _ in ws:
                    pass
            except websockets.exceptions.ConnectionClosed:
                pass

        server = await websockets.serve(v1_agent, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            bridge = RemoteAudioBridge(audio, user_id="u1")
            res = await bridge.start(f"ws://127.0.0.1:{port}/relay")
            self.assertTrue(res.ok, res)
            self.assertEqual(res.service_id, "old")
            self.assertEqual((hellos[0]["version"], hellos[0]["mode"], hellos[0]["user_id"]), ("2", "bridge", "u1"))
            await bridge.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_system_prompt_is_bounded_at_scale(self):
        from developer_ws.pipeline import build_developer_system_instruction

        prompt = build_developer_system_instruction()
        self.assertIn("1204 registered agents", prompt)
        self.assertNotIn("Filler Agent 17", prompt)
        self.assertLess(len(prompt), 6000)


if __name__ == "__main__":
    unittest.main()
