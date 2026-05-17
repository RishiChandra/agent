"""Direct audio relay: forward uplink PCM to a remote WS, play remote audio back to user.

When active, the local STT/LLM/TTS pipeline is bypassed — frames travel from mic to remote
and back unchanged. Activation is requested by Gemini via the start_remote_audio_bridge tool.

Handshake (before any audio flows):

    main → remote:  {"type":"hello","user_id":"...","version":"1"}
    remote → main:  {"type":"ack","accept":true,"service_id":"...","version":"1"}
                  or {"type":"ack","accept":false,"reason":"..."}

A missing/late/malformed ack is logged distinctly so it's clear whether the remote
didn't pick up, rejected the call, or spoke protocol garbage.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import websockets

from audio_codec import UPLINK_SAMPLE_RATE

from .audio_io import AudioIO

log = logging.getLogger("developer_ws")

OnRemoteClose = Callable[[], Awaitable[None]]

REMOTE_BRIDGE_URL = os.environ.get(
    "DEVELOPER_WS_REMOTE_BRIDGE_URL", "ws://localhost:8001/relay"
)

BRIDGE_PROTOCOL_VERSION = "1"
BRIDGE_ACK_TIMEOUT_S = float(os.environ.get("DEVELOPER_WS_BRIDGE_ACK_TIMEOUT_S", "5.0"))

# Outcome categories used by the pipeline to choose a TTS message.
OUTCOME_OK = "ok"
OUTCOME_ALREADY_ACTIVE = "already_active"
OUTCOME_CONNECT_FAILED = "connect_failed"
OUTCOME_HELLO_SEND_FAILED = "hello_send_failed"
OUTCOME_NO_PICKUP = "no_pickup"
OUTCOME_CLOSED_DURING_HANDSHAKE = "closed_during_handshake"
OUTCOME_MALFORMED_ACK = "malformed_ack"
OUTCOME_PROTOCOL_ERROR = "protocol_error"
OUTCOME_REJECTED = "rejected"
OUTCOME_VERSION_MISMATCH = "version_mismatch"  # logged-only; we still proceed

# Close codes used on the bridge side.
CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_REJECTED = 4403  # custom, "Forbidden"-ish

BYE_SEND_TIMEOUT_S = 0.5


@dataclass
class BridgeStartResult:
    ok: bool
    outcome: str
    detail: str = ""
    service_id: str = ""


class RemoteAudioBridge:
    """Owns a single outbound WebSocket. Idempotent on re-start once closed.

    Constructed by: `developer_websocket_endpoint` in endpoint.py.
    Driven by:
      - `start(url)` — called by `pipeline._handle_tool_call` (Gemini tool) or
        `pipeline.on_service_ping` (HTTP ping).
      - `send_uplink_pcm(pcm)` — called by `_handle_audio` in endpoint.py while
        `bridge.active` is True, instead of feeding the utterance buffer.
      - `close()` — called by `_drain_on_close` and `_handle_interrupt` (user "stop").
    Notifies up:
      - `_on_remote_close` callback (registered by `pipeline.__init__` via
        `set_on_remote_close`) fires when the remote — not us — closes the WS.
    """

    def __init__(self, audio_io: AudioIO, user_id: str = "") -> None:
        self._audio = audio_io
        self._user_id = user_id
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._recv_task: asyncio.Task | None = None
        self._active = False
        # Set this to be notified when the remote (not local) closes the connection.
        self._on_remote_close: Optional[OnRemoteClose] = None
        # True while close() is unwinding; tells _recv_loop's finally not to fire on_remote_close.
        self._self_closing = False
        # Frame counters for diagnostic logging on disconnect.
        self._frames_sent = 0
        self._frames_recv = 0

    def set_on_remote_close(self, cb: Optional[OnRemoteClose]) -> None:
        """Register the callback fired when the remote closes the bridge.

        Called once by: `pipeline.__init__`, passing `self._on_bridge_remote_close`.
        Invoked from: `_recv_loop` finally block (only when `_self_closing` is False).
        """
        self._on_remote_close = cb

    @property
    def active(self) -> bool:
        return self._active

    async def start(self, url: str = REMOTE_BRIDGE_URL) -> BridgeStartResult:
        """Open the outbound WS and complete the hello/ack handshake.

        Called by: `pipeline._handle_tool_call`, `pipeline.on_service_ping`.
        On success: spawns `_recv_loop` as a task and sets `_active = True`.
        On failure: closes the socket and returns an outcome the pipeline maps to
        a TTS message via `_fail_message(result)`.
        """
        if self._active:
            log.info("bridge start: already active user_id=%s", self._user_id)
            return BridgeStartResult(
                ok=True, outcome=OUTCOME_ALREADY_ACTIVE, service_id=""
            )

        # Reset state for a fresh attempt (previous attempt may have left flags set).
        self._self_closing = False
        self._frames_sent = 0
        self._frames_recv = 0

        log.info("bridge connecting user_id=%s url=%s", self._user_id, url)
        try:
            self._ws = await websockets.connect(url, max_size=None)
        except Exception as e:
            log.warning(
                "bridge connect failed user_id=%s url=%s err=%s",
                self._user_id, url, e,
            )
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_CONNECT_FAILED, detail=str(e)
            )

        hello = {
            "type": "hello",
            "user_id": self._user_id,
            "version": BRIDGE_PROTOCOL_VERSION,
        }
        try:
            await self._ws.send(json.dumps(hello))
            log.info("bridge hello sent user_id=%s", self._user_id)
        except Exception as e:
            log.warning(
                "bridge hello send failed user_id=%s err=%s", self._user_id, e,
            )
            await self._safe_close_ws()
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_HELLO_SEND_FAILED, detail=str(e)
            )

        try:
            raw = await asyncio.wait_for(
                self._ws.recv(), timeout=BRIDGE_ACK_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            log.warning(
                "bridge ack TIMEOUT user_id=%s — remote didn't pick up within %.1fs",
                self._user_id, BRIDGE_ACK_TIMEOUT_S,
            )
            await self._safe_close_ws()
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_NO_PICKUP,
                detail=f"no response within {BRIDGE_ACK_TIMEOUT_S}s",
            )
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(
                "bridge closed during handshake user_id=%s code=%s reason=%s",
                self._user_id, getattr(e, "code", "?"), getattr(e, "reason", ""),
            )
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_CLOSED_DURING_HANDSHAKE,
                detail=f"code={getattr(e, 'code', '?')}",
            )
        except Exception as e:
            log.warning(
                "bridge ack recv error user_id=%s err=%s", self._user_id, e,
            )
            await self._safe_close_ws()
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_CLOSED_DURING_HANDSHAKE, detail=str(e)
            )

        try:
            ack = json.loads(raw)
        except (ValueError, TypeError):
            log.warning(
                "bridge ack malformed (not JSON) user_id=%s raw=%r",
                self._user_id, raw[:200] if isinstance(raw, (str, bytes)) else raw,
            )
            await self._safe_close_ws()
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_MALFORMED_ACK, detail="not JSON",
            )

        if not isinstance(ack, dict) or ack.get("type") != "ack":
            log.warning(
                "bridge protocol error: expected ack user_id=%s got=%r",
                self._user_id, ack,
            )
            await self._safe_close_ws()
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_PROTOCOL_ERROR,
                detail=f"expected ack, got {ack!r}",
            )

        if not ack.get("accept", False):
            reason = str(ack.get("reason", "no reason given"))
            log.info(
                "bridge REJECTED by remote user_id=%s reason=%s", self._user_id, reason,
            )
            await self._safe_close_ws()
            return BridgeStartResult(
                ok=False, outcome=OUTCOME_REJECTED, detail=reason,
            )

        service_id = str(ack.get("service_id", ""))
        remote_version = str(ack.get("version", "?"))
        if remote_version != "?" and remote_version != BRIDGE_PROTOCOL_VERSION:
            log.warning(
                "bridge version mismatch user_id=%s ours=%s theirs=%s — proceeding anyway",
                self._user_id, BRIDGE_PROTOCOL_VERSION, remote_version,
            )
        log.info(
            "bridge ACK accepted user_id=%s service_id=%s remote_version=%s",
            self._user_id, service_id, remote_version,
        )
        self._active = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        return BridgeStartResult(ok=True, outcome=OUTCOME_OK, service_id=service_id)

    async def send_uplink_pcm(self, pcm: bytes) -> None:
        """Forward one uplink PCM batch to the remote.

        Called by: `_handle_audio` in endpoint.py for every audio frame while
        `bridge.active`. No-ops if not active. On send failure, triggers `close()`
        (which fires the remote-close callback path if the remote was the one to drop).
        """
        if not self._active or self._ws is None or not pcm:
            return
        try:
            await self._ws.send(json.dumps({
                "audio": base64.b64encode(pcm).decode("utf-8"),
                "sr": UPLINK_SAMPLE_RATE,
                "turn_complete": False,
            }))
            self._frames_sent += 1
        except Exception as e:
            log.warning(
                "bridge send failed user_id=%s sent=%d recv=%d err=%s",
                self._user_id, self._frames_sent, self._frames_recv, e,
            )
            await self.close()

    async def _recv_loop(self) -> None:
        """Pull remote frames forever; queue audio for downlink, handle `bye` control frames.

        Spawned by: `start()` on a successful ack.
        Calls: `audio.add_playback_pcm(pcm)` for each audio frame from the remote.
        On exit (remote close, error, or `bye`): if `_self_closing` is False and we
        were previously active, fires the registered `_on_remote_close` callback —
        which is `pipeline._on_bridge_remote_close` in normal use.
        """
        disconnect_reason = "loop_exit"
        try:
            assert self._ws is not None
            async for raw in self._ws:
                # Control frames (e.g., bye) are handled inline; everything else
                # is treated as audio if it has an `audio` field.
                if isinstance(raw, str):
                    try:
                        ctrl = json.loads(raw)
                        if isinstance(ctrl, dict) and ctrl.get("type") == "bye":
                            disconnect_reason = (
                                f"remote_bye reason={ctrl.get('reason', '')!r}"
                            )
                            log.info(
                                "bridge received bye user_id=%s reason=%s",
                                self._user_id, ctrl.get("reason", ""),
                            )
                            break
                    except (ValueError, TypeError):
                        pass
                pcm = self._extract_downlink_pcm(raw)
                if pcm:
                    self._frames_recv += 1
                    self._audio.add_playback_pcm(pcm)
                    # Flush opus residual after every remote chunk so the trailing
                    # sub-frame ships now instead of waiting for the next chunk
                    # (each 16k→24k resampled chunk leaves a partial 40ms frame).
                    self._audio.mark_turn_complete()
        except websockets.exceptions.ConnectionClosed as e:
            disconnect_reason = (
                f"remote_closed code={getattr(e, 'code', '?')} "
                f"reason={getattr(e, 'reason', '')!r}"
            )
            log.info(
                "bridge remote closed user_id=%s %s", self._user_id, disconnect_reason,
            )
        except Exception as e:
            disconnect_reason = f"error: {e}"
            log.warning(
                "bridge recv loop error user_id=%s err=%s", self._user_id, e,
            )
        finally:
            was_active = self._active
            self._active = False
            log.info(
                "bridge session ended user_id=%s self_closing=%s sent=%d recv=%d reason=%s",
                self._user_id, self._self_closing,
                self._frames_sent, self._frames_recv, disconnect_reason,
            )
            # mark_turn_complete here flushes any partial Opus residual so the final
            # remote chunk reaches the user even if the remote closes mid-stream.
            self._audio.mark_turn_complete()
            if was_active and not self._self_closing and self._on_remote_close is not None:
                try:
                    await self._on_remote_close()
                except Exception:
                    log.exception(
                        "bridge on_remote_close raised user_id=%s", self._user_id,
                    )

    @staticmethod
    def _extract_downlink_pcm(raw) -> bytes | None:
        """Pull int16 PCM out of a remote message. Accepts JSON `{audio: base64}` or raw bytes."""
        if isinstance(raw, bytes):
            return raw
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return None
        # Ignore non-audio control frames silently (e.g., spurious acks).
        if not isinstance(msg, dict):
            return None
        audio_b64 = msg.get("audio")
        if not audio_b64:
            return None
        try:
            return base64.b64decode(audio_b64)
        except Exception:
            return None

    async def _safe_close_ws(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def close(self, reason: str = "local_close") -> None:
        """Local-initiated bridge teardown. Sends `bye`, cancels recv task, closes WS.

        Called by: `_drain_on_close` (socket shutdown), `_handle_interrupt` (user said
        "stop"), and `send_uplink_pcm` on send failure.
        Sets `_self_closing` first so the recv loop's finally block knows NOT to fire
        `_on_remote_close` (we're the closer, not the remote).
        """
        self._self_closing = True
        self._active = False
        # Best-effort goodbye so the remote sees an intentional close instead of a hangup.
        if self._ws is not None:
            try:
                await asyncio.wait_for(
                    self._ws.send(json.dumps({"type": "bye", "reason": reason})),
                    timeout=BYE_SEND_TIMEOUT_S,
                )
            except Exception:
                # Remote may already be gone; not interesting.
                pass
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        self._recv_task = None
        await self._safe_close_ws()
        log.info(
            "bridge closed (local) user_id=%s reason=%s sent=%d recv=%d",
            self._user_id, reason, self._frames_sent, self._frames_recv,
        )
