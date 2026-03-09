"""Shared audio utilities for test WebSocket clients."""

import math
import struct

import pyaudio

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
