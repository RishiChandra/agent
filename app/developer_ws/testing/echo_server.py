"""Standalone WebSocket echo server for testing the developer_ws bridge.

Listens on a separate port from main.py and echoes each uplink audio batch back
as a downlink frame. Uplink PCM is 16 kHz mono int16; local playback wants
24 kHz, so we resample on the fly to avoid chipmunk-pitch playback.

Run from the `app/` directory while main.py is also running:

    python developer_ws/testing/echo_server.py

Pass --ping <user_id> to have the echo server POST to main on startup, which
asks Gemini to announce "your service wants to speak with you" and open the
bridge automatically:

    python developer_ws/testing/echo_server.py --ping 2ba330c0-a999-46f8-ba2c-855880bdcf5b

Override the port by setting ECHO_SERVER_PORT.
Override main's URL with MAIN_HTTP_BASE (default http://localhost:8000).
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import logging
import os
import uuid

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

log = logging.getLogger("echo_server")

UPLINK_SR = 16000
DOWNLINK_SR = 24000

# Slice each resampled downlink chunk into small frames so the bridge pumps audio
# to the user with minimal pacing latency. 60 ms @ 24 kHz mono int16 = 2880 bytes.
ECHO_SLICE_MS = int(os.environ.get("ECHO_SLICE_MS", "60"))
ECHO_SLICE_BYTES = DOWNLINK_SR * ECHO_SLICE_MS // 1000 * 2

PROTOCOL_VERSION = "1"
# A per-process identifier so each run is distinguishable in main's logs.
# Override via --service-id or ECHO_SERVICE_ID.
SERVICE_ID = os.environ.get("ECHO_SERVICE_ID") or f"echo-server-{uuid.uuid4().hex[:8]}"
HELLO_TIMEOUT_S = float(os.environ.get("ECHO_HELLO_TIMEOUT_S", "5.0"))

# Close codes (mirrored on the bridge side).
CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_REJECTED = 4403

app = FastAPI()


def _should_reject() -> tuple[bool, str]:
    """Hook for future call-rejection policy. Today: always accept.

    Set ECHO_REJECT_ALL=1 to flip this — useful for testing the rejection path
    end-to-end without changing code.
    """
    if os.environ.get("ECHO_REJECT_ALL", "0").lower() in ("1", "true", "yes"):
        return True, os.environ.get("ECHO_REJECT_REASON", "service unavailable")
    return False, ""


async def _read_hello(websocket: WebSocket, peer: str) -> dict | None:
    """Wait for the bridge's hello frame. Return parsed dict or None on any failure
    (every failure path logs why so misconnects are obvious)."""
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=HELLO_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        log.warning(
            "echo: hello TIMEOUT peer=%s — client connected but didn't send hello within %.1fs",
            peer, HELLO_TIMEOUT_S,
        )
        return None
    except WebSocketDisconnect:
        log.info("echo: peer disconnected BEFORE hello peer=%s", peer)
        return None
    except Exception as e:
        log.warning("echo: hello recv error peer=%s err=%s", peer, e)
        return None

    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("echo: hello not JSON peer=%s raw=%r", peer, raw[:200])
        return None
    if not isinstance(msg, dict) or msg.get("type") != "hello":
        log.warning("echo: expected hello, got peer=%s msg=%r", peer, msg)
        return None
    return msg


@app.websocket("/relay")
async def relay(websocket: WebSocket) -> None:
    await websocket.accept()
    peer = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    log.info("echo: client connected peer=%s", peer)

    hello = await _read_hello(websocket, peer)
    if hello is None:
        try:
            await websocket.close(code=CLOSE_PROTOCOL_ERROR)
        except Exception:
            pass
        log.info("echo: closed peer=%s (no valid hello)", peer)
        return

    user_id = str(hello.get("user_id", "?"))
    client_version = str(hello.get("version", "?"))
    if client_version != "?" and client_version != PROTOCOL_VERSION:
        log.warning(
            "echo: protocol version mismatch peer=%s ours=%s theirs=%s — proceeding anyway",
            peer, PROTOCOL_VERSION, client_version,
        )
    log.info(
        "echo: hello OK peer=%s user_id=%s client_version=%s",
        peer, user_id, client_version,
    )

    reject, reject_reason = _should_reject()
    if reject:
        log.info(
            "echo: REJECTING call peer=%s user_id=%s reason=%s",
            peer, user_id, reject_reason,
        )
        try:
            await websocket.send_text(
                json.dumps({"type": "ack", "accept": False, "reason": reject_reason})
            )
        except Exception as e:
            log.warning("echo: ack(reject) send failed peer=%s err=%s", peer, e)
        try:
            await websocket.close(code=CLOSE_REJECTED)
        except Exception:
            pass
        return

    try:
        await websocket.send_text(
            json.dumps({
                "type": "ack",
                "accept": True,
                "service_id": SERVICE_ID,
                "version": PROTOCOL_VERSION,
            })
        )
    except Exception as e:
        log.warning("echo: ack(accept) send failed peer=%s err=%s", peer, e)
        return
    log.info("echo: ACK sent (accept) peer=%s user_id=%s", peer, user_id)

    resample_state = None
    frames_in = 0
    frames_out = 0
    disconnect_reason = "loop_exit"
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                log.debug("echo: dropping non-JSON frame peer=%s", peer)
                continue
            if not isinstance(msg, dict):
                continue

            # Control frames.
            msg_type = msg.get("type")
            if msg_type == "bye":
                disconnect_reason = f"client_bye reason={msg.get('reason', '')!r}"
                log.info(
                    "echo: bye received peer=%s user_id=%s reason=%s",
                    peer, user_id, msg.get("reason", ""),
                )
                try:
                    await websocket.send_text(
                        json.dumps({"type": "bye", "reason": "ack"})
                    )
                    await websocket.close(code=CLOSE_NORMAL)
                except Exception:
                    pass
                break
            if msg_type == "hello":
                # Idempotent: ignore extra hello after the first.
                log.debug("echo: duplicate hello peer=%s — ignoring", peer)
                continue

            audio_b64 = msg.get("audio")
            if not audio_b64:
                continue
            try:
                pcm = base64.b64decode(audio_b64)
            except Exception:
                log.debug("echo: dropping non-base64 audio frame peer=%s", peer)
                continue
            frames_in += 1
            in_sr = int(msg.get("sr", UPLINK_SR))
            out_pcm, resample_state = audioop.ratecv(
                pcm, 2, 1, in_sr, DOWNLINK_SR, resample_state
            )
            # Split into small slices so the bridge sees frequent downlink frames
            # instead of one big 1.5s blob — keeps server-side coalescing cheap
            # and lets audio start playing sooner.
            try:
                send_failed = False
                for i in range(0, len(out_pcm), ECHO_SLICE_BYTES):
                    slice_pcm = out_pcm[i : i + ECHO_SLICE_BYTES]
                    if not slice_pcm:
                        continue
                    await websocket.send_text(
                        json.dumps({
                            "audio": base64.b64encode(slice_pcm).decode("utf-8"),
                            "sr": DOWNLINK_SR,
                        })
                    )
                    frames_out += 1
            except Exception as e:
                send_failed = True
                disconnect_reason = f"send_error: {e}"
                log.warning(
                    "echo: send failed peer=%s user_id=%s err=%s (in=%d out=%d)",
                    peer, user_id, e, frames_in, frames_out,
                )
            if send_failed:
                break
    except WebSocketDisconnect as e:
        disconnect_reason = f"client_closed code={getattr(e, 'code', '?')}"
    except Exception as e:
        disconnect_reason = f"error: {e}"
        log.warning(
            "echo: relay error peer=%s user_id=%s err=%s", peer, user_id, e,
        )
    finally:
        log.info(
            "echo: session ended peer=%s user_id=%s frames_in=%d frames_out=%d reason=%s",
            peer, user_id, frames_in, frames_out, disconnect_reason,
        )


async def _ping_main(user_id: str, base: str, delay_s: float, service_id: str) -> None:
    """POST to main's /developer/ping/{user_id} after a short delay so it has time to start.

    Body carries our service_id so main can log/verify which service is calling
    before it dials back over the bridge.
    """
    await asyncio.sleep(delay_s)
    url = f"{base.rstrip('/')}/developer/ping/{user_id}"
    payload = {
        "service_id": service_id,
        "version": PROTOCOL_VERSION,
    }
    log.info("pinging main: POST %s body=%s", url, payload)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload)
            log.info("ping response status=%d body=%s", r.status_code, r.text)
    except Exception as e:
        log.warning("ping failed: %s", e)


@app.on_event("startup")
async def _maybe_schedule_ping() -> None:
    user_id = app.state.ping_user_id if hasattr(app.state, "ping_user_id") else None
    if not user_id:
        return
    base = os.environ.get("MAIN_HTTP_BASE", "http://localhost:8000")
    delay = float(os.environ.get("PING_DELAY_S", "1.0"))
    asyncio.create_task(_ping_main(user_id, base, delay, SERVICE_ID))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ping",
        metavar="USER_ID",
        help="After startup, POST to main's /developer/ping/{USER_ID} to trigger the announcement + auto-bridge.",
    )
    parser.add_argument(
        "--service-id",
        metavar="ID",
        default=None,
        help=f"Override the service_id sent in pings + acks. Defaults to {SERVICE_ID}.",
    )
    args = parser.parse_args()
    app.state.ping_user_id = args.ping
    if args.service_id:
        SERVICE_ID = args.service_id

    port = int(os.environ.get("ECHO_SERVER_PORT", "8001"))
    log.info(
        "echo server listening on ws://0.0.0.0:%d/relay service_id=%s",
        port, SERVICE_ID,
    )
    if args.ping:
        log.info(
            "will ping main for user_id=%s as service_id=%s after startup",
            args.ping, SERVICE_ID,
        )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        ws="websockets",
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
