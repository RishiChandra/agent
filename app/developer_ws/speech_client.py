"""Offline speech for developer_ws: Vosk STT + pyttsx3 (system) TTS — no cloud or Gemini."""

from __future__ import annotations

import asyncio
import audioop
import json
import os
import tempfile
import threading
import wave

from dotenv import load_dotenv

from audio_codec import DOWNLINK_SAMPLE_RATE

load_dotenv()

_vosk_model_instance = None
_vosk_model_path: str | None = None
_vosk_lock = threading.Lock()


def _get_vosk_model():
    """Lazy-load Vosk model from VOSK_MODEL_PATH (directory of an unpacked model)."""
    global _vosk_model_instance, _vosk_model_path
    path = os.environ.get("VOSK_MODEL_PATH", "").strip()
    if not path or not os.path.isdir(path):
        return None
    with _vosk_lock:
        if _vosk_model_instance is not None and _vosk_model_path == path:
            return _vosk_model_instance
        try:
            from vosk import Model
        except ImportError:
            print(
                "[developer_ws] vosk not installed. pip install vosk "
                "and set VOSK_MODEL_PATH to an unpacked model from https://alphacephei.com/vosk/models"
            )
            return None
        _vosk_model_instance = Model(path)
        _vosk_model_path = path
        return _vosk_model_instance


def transcribe_pcm16_sync(pcm: bytes, sample_rate: int) -> str:
    """Transcribe mono int16 PCM using Vosk (fully offline)."""
    sr = int(sample_rate)
    min_bytes = int(sr * 0.08 * 2)  # ~80 ms minimum
    if len(pcm) < min_bytes:
        return ""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    model = _get_vosk_model()
    if model is None:
        print(
            "[developer_ws] Set VOSK_MODEL_PATH to an unpacked Vosk model directory "
            "(e.g. vosk-model-small-en-us-0.15). See https://alphacephei.com/vosk/models"
        )
        return ""
    try:
        from vosk import KaldiRecognizer
    except ImportError:
        return ""

    # Trailing silence helps the decoder finalize the last word.
    pad_ms = int(os.environ.get("VOSK_END_PAD_MS", "400"))
    pad_bytes = max(0, int(sr * pad_ms / 1000) * 2)
    pcm_in = pcm + (b"\x00" * pad_bytes)

    rec = KaldiRecognizer(model, sr)
    # Whole-buffer feed avoids oddities at small fixed chunk boundaries.
    if len(pcm_in) <= 256000:
        rec.AcceptWaveform(pcm_in)
    else:
        step = 8000
        for i in range(0, len(pcm_in), step):
            rec.AcceptWaveform(pcm_in[i : i + step])
    try:
        payload = json.loads(rec.FinalResult())
    except json.JSONDecodeError:
        return ""
    return (payload.get("text") or "").strip()


def tts_to_pcm24_sync(text: str) -> bytes:
    """Synthesize speech via pyttsx3 (OS engine), return mono int16 PCM at DOWNLINK_SAMPLE_RATE."""
    t = text.strip()
    if not t:
        return b""
    try:
        import pyttsx3
    except ImportError:
        print("[developer_ws] pyttsx3 not installed. pip install pyttsx3")
        return b""

    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        engine = pyttsx3.init()
        engine.save_to_file(t, wav_path)
        engine.runAndWait()

        with wave.open(wav_path, "rb") as wf:
            ch = wf.getnchannels()
            sr = wf.getframerate()
            sw = wf.getsampwidth()
            pcm = wf.readframes(wf.getnframes())

        if sw != 2:
            print(f"[developer_ws] pyttsx3 produced {sw*8}-bit audio; expected 16-bit WAV")
            return b""
        if ch == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        elif ch != 1:
            return b""

        if sr == DOWNLINK_SAMPLE_RATE:
            return pcm
        pcm24, _ = audioop.ratecv(pcm, 2, 1, sr, DOWNLINK_SAMPLE_RATE, None)
        return pcm24
    except Exception as e:
        print(f"[developer_ws] pyttsx3 TTS failed: {e}")
        return b""
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


async def transcribe_pcm16(pcm: bytes, sample_rate: int) -> str:
    return await asyncio.to_thread(transcribe_pcm16_sync, pcm, sample_rate)


async def synthesize_speech_pcm24(text: str) -> bytes:
    return await asyncio.to_thread(tts_to_pcm24_sync, text)
