"""End-to-end local test runner for the developer-WS audio path.

Brings up three processes in the right order, with health checks between each,
and tears them all down on Ctrl-C or when any one exits.

    1. main.py                       (FastAPI/uvicorn on :8000)
    2. test_developer_ws.py          (mic + WS client; talks to main)
    3. echo_server.py --ping <id>    (relay on :8001; pings main → bridge dials back)

Run from the repo root (where the .venv lives) or anywhere — paths are absolute:

    python app/developer_ws/testing/run_full_test.py

Override defaults via env or flags:
    USER_ID, MAIN_PORT, ECHO_PORT, CLIENT_WARMUP_S, READY_TIMEOUT_S
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

THIS = Path(__file__).resolve()
APP_DIR = THIS.parents[2]                      # .../agent/app
REPO_DIR = APP_DIR.parent                       # .../agent
MAIN_PY = APP_DIR / "main.py"
ECHO_PY = APP_DIR / "developer_ws" / "testing" / "echo_server.py"
TEST_CLIENT_PY = REPO_DIR / "test" / "app" / "developer" / "test_developer_ws.py"

DEFAULT_USER_ID = "2ba330c0-a999-46f8-ba2c-855880bdcf5b"
PYTHON = sys.executable


def _wait_for_port(host: str, port: int, timeout: float, label: str) -> bool:
    """Poll until something accepts on host:port (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            try:
                s.connect((host, port))
                return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.4)
    print(f"[runner] {label} did not start listening on {host}:{port} within {timeout:.0f}s")
    return False


def _spawn(label: str, args: list[str], cwd: Path, separate_console: bool) -> subprocess.Popen:
    print(f"[runner] starting {label}: {' '.join(args)}  (cwd={cwd})")
    popen_kwargs: dict = {"cwd": str(cwd)}
    if separate_console and sys.platform == "win32":
        # Open each child in its own console window so logs don't interleave and
        # you can Ctrl-C any one independently.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    return subprocess.Popen(args, **popen_kwargs)


def _stop(label: str, p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    print(f"[runner] stopping {label} (pid={p.pid})")
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print(f"[runner] force-killing {label}")
        p.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default=os.environ.get("USER_ID", DEFAULT_USER_ID))
    parser.add_argument("--main-port", type=int, default=int(os.environ.get("MAIN_PORT", "8000")))
    parser.add_argument("--echo-port", type=int, default=int(os.environ.get("ECHO_PORT", "8001")))
    parser.add_argument(
        "--client-warmup",
        type=float,
        default=float(os.environ.get("CLIENT_WARMUP_S", "3.0")),
        help="Seconds to let the test client connect before launching the echo server.",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=float(os.environ.get("READY_TIMEOUT_S", "60")),
        help="Seconds to wait for main and echo to start listening.",
    )
    parser.add_argument(
        "--no-ping",
        action="store_true",
        help="Start the echo server without --ping so the bridge only opens when the user asks.",
    )
    parser.add_argument(
        "--inline",
        action="store_true",
        help="Share this terminal across all three children (no separate consoles).",
    )
    args = parser.parse_args()
    separate_console = (not args.inline) and (sys.platform == "win32")

    for p in (MAIN_PY, ECHO_PY, TEST_CLIENT_PY):
        if not p.exists():
            print(f"[runner] missing required file: {p}")
            return 2

    env_overrides = {"DEVELOPER_WS_USER_ID": args.user_id}
    os.environ.update(env_overrides)

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        # 1. main.py
        procs.append(("main", _spawn("main.py", [PYTHON, str(MAIN_PY)], APP_DIR, separate_console)))
        if not _wait_for_port("127.0.0.1", args.main_port, args.ready_timeout, "main"):
            return 1
        print(f"[runner] main is up on :{args.main_port}")

        # 2. test client
        procs.append((
            "client",
            _spawn("test client", [PYTHON, str(TEST_CLIENT_PY)], REPO_DIR, separate_console),
        ))
        print(f"[runner] giving client {args.client_warmup:.1f}s to connect ...")
        time.sleep(args.client_warmup)
        if procs[-1][1].poll() is not None:
            print(f"[runner] client exited early with code {procs[-1][1].returncode}")
            return 1

        # 3. echo server (with --ping unless suppressed)
        echo_args = [PYTHON, str(ECHO_PY)]
        if not args.no_ping:
            echo_args += ["--ping", args.user_id]
        procs.append(("echo", _spawn("echo server", echo_args, APP_DIR, separate_console)))
        _wait_for_port("127.0.0.1", args.echo_port, 20.0, "echo")

        print(
            "\n[runner] all three components running.\n"
            "[runner]   - speak into your mic; say 'call the service' or wait for the ping-triggered call.\n"
            "[runner]   - Ctrl-C in a child window kills only that child; the run continues.\n"
            "[runner]   - the run ends when the test client exits (or Ctrl-C here).\n"
        )

        # Watchdog: only the test client gates the run. Main/echo can die without
        # taking the session down — we just log it so it's visible.
        client_proc = next((p for label, p in procs if label == "client"), None)
        already_logged: set[str] = set()
        while True:
            if client_proc is not None and client_proc.poll() is not None:
                print(
                    f"\n[runner] client exited with code {client_proc.returncode}; "
                    f"tearing down the rest."
                )
                return client_proc.returncode or 0
            for label, p in procs:
                if label == "client":
                    continue
                if p.poll() is not None and label not in already_logged:
                    print(
                        f"[runner] {label} exited with code {p.returncode} "
                        f"(not fatal; client still running)."
                    )
                    already_logged.add(label)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[runner] Ctrl-C — shutting down.")
        return 0
    finally:
        for label, p in reversed(procs):
            _stop(label, p)


if __name__ == "__main__":
    raise SystemExit(main())
