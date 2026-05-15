"""Direct audio relay: forward uplink PCM to a remote WS, play remote audio back to user.

When active, the local STT/LLM/TTS pipeline is bypassed — frames travel from mic to remote
and back unchanged. Activation is requested by Gemini via the start_remote_audio_bridge tool.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import websockets

from audio_codec import UPLINK_SAMPLE_RATE

from .audio_io import AudioIO

log = logging.getLogger("developer_ws")

# TODO: replace with real relay URL once the remote side exists.
REMOTE_BRIDGE_URL = "wss://example-remote.invalid/relay"


class RemoteAudioBridge:
    """Owns a single outbound WebSocket; one-shot — call `close()` and create a new instance to retry."""

    def __init__(self, audio_io: AudioIO) -> None:
        self._audio = audio_io
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._recv_task: asyncio.Task | None = None
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    async def start(self, url: str = REMOTE_BRIDGE_URL) -> bool:
        """Open the outbound WS. Returns True on success, False on connect failure."""
        if self._active:
            return True
        try:
            self._ws = await websockets.connect(url, max_size=None)
        except Exception as e:
            log.warning("bridge connect failed url=%s: %s", url, e)
            return False
        self._active = True
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("bridge connected url=%s", url)
        return True

    async def send_uplink_pcm(self, pcm: bytes) -> None:
        """Forward one uplink PCM batch to the remote. Mirrors the developer-WS message shape."""
        if not self._active or self._ws is None or not pcm:
            return
        try:
            await self._ws.send(json.dumps({
                "audio": base64.b64encode(pcm).decode("utf-8"),
                "sr": UPLINK_SAMPLE_RATE,
                "turn_complete": False,
            }))
        except Exception as e:
            log.warning("bridge send failed: %s", e)
            await self.close()

    async def _recv_loop(self) -> None:
        """Pull remote audio frames and push them straight to the local downlink queue."""
        try:
            assert self._ws is not None
            async for raw in self._ws:
                pcm = self._extract_downlink_pcm(raw)
                if pcm:
                    self._audio.add_playback_pcm(pcm)
        except websockets.exceptions.ConnectionClosed:
            log.info("bridge remote closed")
        except Exception as e:
            log.warning("bridge recv loop error: %s", e)
        finally:
            self._active = False
            # mark_turn_complete here flushes any partial Opus residual so the final
            # remote chunk reaches the user even if the remote closes mid-stream.
            self._audio.mark_turn_complete()

    @staticmethod
    def _extract_downlink_pcm(raw) -> bytes | None:
        """Pull int16 PCM out of a remote message. Accepts JSON `{audio: base64}` or raw bytes."""
        if isinstance(raw, bytes):
            return raw
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return None
        audio_b64 = msg.get("audio")
        if not audio_b64:
            return None
        try:
            return base64.b64decode(audio_b64)
        except Exception:
            return None

    async def close(self) -> None:
        self._active = False
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        self._recv_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        log.info("bridge closed")
