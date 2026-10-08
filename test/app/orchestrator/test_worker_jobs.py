"""Worker handling of the orchestrator job kinds (ORCHESTRATOR_V2_TOOL_CALLS.md §1.6).

* `agent_task_deadline` POSTs the task context to the orchestrator's internal
  hook (never wakes the device; 5xx and connection errors are retried).
* `task_result` wakes the pin, unless the result was already delivered or a
  session is active.

Needs `pgserver`. The worker's MQTT publish and HTTP call are stubbed.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import unittest
import urllib.error
import uuid
from unittest.mock import patch

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import pgfix  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "../../.."))


def load_worker():
    """Import listener/worker.py with listener/ first on sys.path (it has its own `database`)."""
    saved = {k: sys.modules.pop(k) for k in ("database", "session_management_utils", "mqtt_publish") if k in sys.modules}
    sys.path.insert(0, os.path.join(ROOT, "listener"))
    try:
        sys.modules.pop("worker", None)
        return importlib.import_module("worker")
    finally:
        sys.path.remove(os.path.join(ROOT, "listener"))
        for k, v in saved.items():
            sys.modules[k] = v


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class WorkerJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pgfix.fresh_database()
        os.environ["INTERNAL_API_TOKEN"] = "itok"
        cls.worker = load_worker()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("INTERNAL_API_TOKEN", None)

    def setUp(self):
        self.user = pgfix.add_user()
        pgfix.query("INSERT INTO sessions (user_id, is_active) VALUES (%s, false)", (self.user,))

    def _agent_task(self, delivered=False):
        aid, tid = str(uuid.uuid4()), str(uuid.uuid4())
        pgfix.query("INSERT INTO agents (agent_id, agent_info, agent_url) VALUES (%s, '{}', 'ws://x')", (aid,))
        pgfix.query(
            "INSERT INTO tasks (task_id, user_id, kind, agent_id, status, delivered_at) "
            "VALUES (%s, %s, 'agent_task', %s, 'completed', CASE WHEN %s THEN now() END)",
            (tid, self.user, aid, delivered),
        )
        return tid

    def test_deadline_job_posts_context_with_internal_token(self):
        seen = {}

        def fake_urlopen(req, timeout=0):
            seen["url"], seen["auth"], seen["body"] = req.full_url, req.headers.get("Authorization"), json.loads(req.data)
            return FakeResp(b'{"ok": true, "outcome": "overdue"}')

        job = {"id": 1, "kind": "agent_task_deadline", "attempts": 0,
               "payload": {"task_id": "t1", "intent": "book", "deadline_at": "2026-10-07T19:00:00Z"}}
        with patch.object(self.worker.urllib.request, "urlopen", fake_urlopen), \
                patch.object(self.worker, "send_to_device") as wake:
            self.assertTrue(self.worker.handle_job(job))
        self.assertEqual(seen["url"], "http://app:8000/internal/tasks/t1/deadline")
        self.assertEqual(seen["auth"], "Bearer itok")
        self.assertEqual(seen["body"]["intent"], "book")
        wake.assert_not_called()

    def test_deadline_job_connection_error_is_transient(self):
        def refused(req, timeout=0):
            raise urllib.error.URLError("connection refused")

        job = {"id": 2, "kind": "agent_task_deadline", "attempts": 0, "payload": {"task_id": "t1"}}
        with patch.object(self.worker.urllib.request, "urlopen", refused):
            with self.assertRaises(OSError):  # process_batch retries OSError without spending an attempt
                self.worker.handle_job(job)

    def test_deadline_job_http_error_spends_an_attempt(self):
        def unavailable(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 503, "token not configured", {}, io.BytesIO(b"{}"))

        job = {"id": 6, "kind": "agent_task_deadline", "attempts": 0, "payload": {"task_id": "t1"}}
        with patch.object(self.worker.urllib.request, "urlopen", unavailable):
            with self.assertRaises(RuntimeError) as ctx:
                self.worker.handle_job(job)
            self.assertNotIsInstance(ctx.exception, OSError)

    def test_task_result_wakes_device(self):
        tid = self._agent_task()
        job = {"id": 3, "kind": "task_result", "attempts": 0, "payload": {"user_id": self.user, "task_id": tid}}
        with patch.object(self.worker, "send_to_device") as wake:
            self.assertTrue(self.worker.handle_job(job))
        wake.assert_called_once()
        cmd = wake.call_args[0][1]
        self.assertEqual((cmd["command"], cmd["reason"]), ("start_websocket", "task_result"))

    def test_task_result_dropped_when_already_delivered(self):
        tid = self._agent_task(delivered=True)
        job = {"id": 4, "kind": "task_result", "attempts": 0, "payload": {"user_id": self.user, "task_id": tid}}
        with patch.object(self.worker, "send_to_device") as wake:
            self.assertTrue(self.worker.handle_job(job))
        wake.assert_not_called()

    def test_task_result_deferred_while_session_active(self):
        tid = self._agent_task()
        pgfix.query("UPDATE sessions SET is_active = true WHERE user_id = %s", (self.user,))
        job = {"id": 5, "kind": "task_result", "attempts": 0, "payload": {"user_id": self.user, "task_id": tid}}
        with patch.object(self.worker, "send_to_device") as wake:
            self.assertFalse(self.worker.handle_job(job))
        wake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
