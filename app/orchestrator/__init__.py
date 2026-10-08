"""The orchestrator: the voice session at /ws/developer/{user_id}, agent routing,
and background tasks. See README.md in this directory for the layout.

Exports are resolved lazily (PEP 562) so that importing a light submodule,
e.g. `orchestrator.tasks.store` from the HTTP routes or the registry, does not
pull in Pipecat, Vosk or Piper.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "AudioIO": ".audio_io",
    "developer_websocket_endpoint": ".endpoint",
    "preload_vosk_model": ".stt",
    "warm_recognizer_pool_with_tts": ".stt",
    "preload_piper_voice": ".tts",
    "preload_silero_vad": ".vad",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'orchestrator' has no attribute {name!r}")
    return getattr(importlib.import_module(module, __name__), name)
