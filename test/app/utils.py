"""Shared audio utilities for test WebSocket clients."""

import base64
import math
import struct
from typing import Any

import pyaudio

# Downlink matches app/audio_manager: Gemini 24 kHz mono int16, 40 ms Opus frames.
_DOWNLINK_SAMPLE_RATE = 24000
_DOWNLINK_CHANNELS = 1
_DOWNLINK_FRAME_SAMPLES = _DOWNLINK_SAMPLE_RATE * 40 // 1000  # 960


# Log once: downlink Opus needs opuslib + native libopus (opus.dll on Windows).
_opus_downlink_unavailable_logged = False


def _unpack_opus_tlv(tlv: bytes) -> list[bytes]:
    """Same layout as app.audio_manager: [u16 BE len][opus_bytes]... per frame."""
    out: list[bytes] = []
    i = 0
    while i + 2 <= len(tlv):
        n = struct.unpack(">H", tlv[i : i + 2])[0]
        i += 2
        if i + n > len(tlv):
            break
        out.append(tlv[i : i + n])
        i += n
    return out


def decode_websocket_audio_for_playback(data: dict[str, Any]) -> bytes:
    """Decode server `audio` JSON field to int16 PCM for PyAudio at 24 kHz mono.

    The WebSocket sends Opus packets in a TLV blob when ``codec`` is ``opus``;
    otherwise the payload is legacy raw PCM.

    If ``opuslib`` or the native Opus library (e.g. ``opus.dll`` on Windows) is
    missing, logs once and returns empty bytes so callers can keep receiving text.
    """
    global _opus_downlink_unavailable_logged

    blob = base64.b64decode(data["audio"])
    if data.get("codec") != "opus":
        return blob
    try:
        import opuslib

        decoder = opuslib.Decoder(_DOWNLINK_SAMPLE_RATE, _DOWNLINK_CHANNELS)
        pcm = bytearray()
        for pkt in _unpack_opus_tlv(blob):
            pcm += decoder.decode(pkt, _DOWNLINK_FRAME_SAMPLES, decode_fec=False)
        return bytes(pcm)
    except (ImportError, OSError) as e:
        if not _opus_downlink_unavailable_logged:
            _opus_downlink_unavailable_logged = True
            hint = (
                "This interpreter has no opuslib package (wrong venv?). "
                "Try: pip install -r test/requirements.txt or pip install opuslib."
                if isinstance(e, ModuleNotFoundError)
                else "Install: python -m pip install opuslib\n"
                "   On Windows you also need opus.dll — run scripts/install_opus_windows.py with this venv’s python.exe."
            )
            print(
                "⚠️ Cannot decode agent audio (Opus): opuslib or native libopus failed to load.\n"
                f"   ({e})\n"
                f"   {hint}\n"
                "   Skipping TTS playback; transcriptions and mic still work."
            )
        return b""
    except Exception as e:
        # opuslib raises plain Exception("Could not find Opus library...") when opus.dll is missing.
        el = str(e).lower()
        if "opus" in el or "could not find" in el:
            if not _opus_downlink_unavailable_logged:
                _opus_downlink_unavailable_logged = True
                print(
                    "⚠️ Cannot decode agent audio (Opus): native libopus is missing (on Windows: opus.dll).\n"
                    f"   ({e})\n"
                    "   pip installs only the Python wrapper; install Opus (e.g. conda install -c conda-forge opus)\n"
                    "   and ensure opus.dll is on PATH or next to python.exe.\n"
                    "   Skipping TTS playback; text still works."
                )
            return b""
        raise

FORMAT = pyaudio.paInt16
CHANNELS = 1
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK = 512
TONE_RATE = 24000


def _generate_tone(freq: float, duration: float, rate: int = TONE_RATE, fade_ms: int = 10) -> bytes:
    """Generate a sine-wave tone as PCM int16 bytes with short fade in/out."""
    num_samples = int(rate * duration)
    fade_samples = int(rate * fade_ms / 1000)
    samples = []
    for i in range(num_samples):
        t = i / rate
        amplitude = 32767 * 0.5
        sample = amplitude * math.sin(2 * math.pi * freq * t)
        if i < fade_samples:
            sample *= i / fade_samples
        elif i >= num_samples - fade_samples:
            sample *= (num_samples - i) / fade_samples
        samples.append(int(sample))
    return struct.pack(f"<{num_samples}h", *samples)


def _connection_ring(p: pyaudio.PyAudio):
    """Play a two-tone ascending chime (connection)."""
    stream = p.open(format=FORMAT, channels=CHANNELS, rate=TONE_RATE, output=True)
    try:
        stream.write(_generate_tone(880, 0.12))   # A5
        stream.write(_generate_tone(1174, 0.18))  # D6
    finally:
        stream.stop_stream()
        stream.close()


def _disconnection_ring(p: pyaudio.PyAudio):
    """Play a two-tone descending chime (disconnection)."""
    stream = p.open(format=FORMAT, channels=CHANNELS, rate=TONE_RATE, output=True)
    try:
        stream.write(_generate_tone(1174, 0.12))  # D6
        stream.write(_generate_tone(587, 0.22))   # D5
    finally:
        stream.stop_stream()
        stream.close()
