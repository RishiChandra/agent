"""Developer WebSocket subsystem: voice-loop over WS, with optional bridge to a remote service.

Inbound mic audio → Vosk STT → Gemini text (with tools) → pyttsx3 TTS → outbound audio.
When Gemini calls `start_remote_audio_bridge`, frames are relayed verbatim to a remote
WebSocket service instead of going through STT/LLM/TTS. See DESIGN.md and BRIDGE_PROTOCOL.md.
"""

from .audio_io import AudioIO
from .endpoint import developer_websocket_endpoint
from .stt import preload_vosk_model
from .tts import preload_piper_voice

__all__ = [
    "AudioIO",
    "developer_websocket_endpoint",
    "preload_vosk_model",
    "preload_piper_voice",
]
