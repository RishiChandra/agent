"""Offline (pyttsx3) text-to-speech.

`synthesize_speech_pcm24(text)` returns int16 mono PCM at the downlink sample rate
(24 kHz), resampling from whatever the OS voice produces. Returned bytes are queued
on `AudioIO` for Opus encoding and downlink delivery.
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import os
import tempfile
import wave

from audio_codec import DOWNLINK_SAMPLE_RATE

log = logging.getLogger("developer_ws")


def _synthesize_sync(text: str) -> bytes:
    """Render text via pyttsx3, return mono int16 PCM at DOWNLINK_SAMPLE_RATE."""
    t = text.strip()
    if not t:
        return b""
    try:
        import pyttsx3
    except ImportError:
        log.warning("pyttsx3 not installed. pip install pyttsx3")
        return b""

    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        engine = pyttsx3.init()
        engine.save_to_file(t, wav_path)
        engine.runAndWait()

        with wave.open(wav_path, "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            sample_width = wf.getsampwidth()
            pcm = wf.readframes(wf.getnframes())

        if sample_width != 2:
            log.warning("pyttsx3 produced %d-bit audio; expected 16-bit WAV", sample_width * 8)
            return b""
        if channels == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        elif channels != 1:
            return b""

        if rate == DOWNLINK_SAMPLE_RATE:
            return pcm
        # Resample to the wire rate so downlink can be Opus-encoded directly.
        resampled, _ = audioop.ratecv(pcm, 2, 1, rate, DOWNLINK_SAMPLE_RATE, None)
        return resampled
    except Exception as e:
        log.warning("pyttsx3 TTS failed: %s", e)
        return b""
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


async def synthesize_speech_pcm24(text: str) -> bytes:
    """Synthesize speech and return int16 mono PCM at the downlink sample rate.

    Called by: `pipeline._speak` after Gemini returns plain text, after a tool ack,
    after a service-ping announcement, and on bridge remote-close notification.
    Offloaded to a worker thread because pyttsx3 is blocking.
    """
    return await asyncio.to_thread(_synthesize_sync, text)
