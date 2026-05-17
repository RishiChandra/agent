"""Shared device audio codec: Opus TLV uplink/downlink and PCM helpers (no transport / queue logic)."""

import audioop
import os
import struct
from typing import List, Optional

try:
    import opuslib

    OPUS_AVAILABLE = True
except (ImportError, OSError, Exception) as _opus_err:
    print(f"⚠️ Opus unavailable, falling back to raw PCM: {_opus_err}")
    opuslib = None  # type: ignore[assignment]
    OPUS_AVAILABLE = False

# Downlink (server → device): mono int16 PCM at this rate before Opus encode.
DOWNLINK_SAMPLE_RATE = 24000
CHANNELS = 1

# Opus frame: 40 ms @ downlink rate (matches device decode cadence).
OPUS_FRAME_MS = 40
OPUS_FRAME_SAMPLES = DOWNLINK_SAMPLE_RATE * OPUS_FRAME_MS // 1000
OPUS_FRAME_BYTES_PCM = OPUS_FRAME_SAMPLES * 2
OPUS_BITRATE = 24000

# Uplink (device → server): 16 kHz mono int16, 20 ms Opus frames typical.
UPLINK_SAMPLE_RATE = 16000
UPLINK_FRAME_MS = 20
UPLINK_FRAME_SAMPLES = UPLINK_SAMPLE_RATE * UPLINK_FRAME_MS // 1000

# Playback bundling (JSON+base64 overhead vs latency).
# Lower → less latency, more frames/sec. Override with DEVELOPER_WS_COALESCE_MS.
COALESCE_TARGET_MS = int(os.environ.get("DEVELOPER_WS_COALESCE_MS", "120"))
COALESCE_WAIT_S = COALESCE_TARGET_MS / 1000

SILENCE_DROP_RMS = 30

# Back-compat alias used by legacy imports.
SAMPLE_RATE = DOWNLINK_SAMPLE_RATE


def rms_int16_le(pcm: bytes) -> int:
    if not pcm:
        return 0
    return audioop.rms(pcm, 2)


def unpack_opus_tlv(tlv: bytes) -> List[bytes]:
    out: List[bytes] = []
    i = 0
    while i + 2 <= len(tlv):
        n = struct.unpack(">H", tlv[i : i + 2])[0]
        i += 2
        if i + n > len(tlv):
            break
        out.append(tlv[i : i + n])
        i += n
    return out


def pack_opus_tlv(opus_packets: List[bytes]) -> bytes:
    out = bytearray()
    for pkt in opus_packets:
        out += struct.pack(">H", len(pkt))
        out += pkt
    return bytes(out)


class UplinkOpusDecoder:
    """Decode TLV-packed uplink Opus frames to int16 mono PCM."""

    def __init__(self) -> None:
        self._decoder: Optional[object] = None
        self._decoder_rate: Optional[int] = None

    def decode_tlv(self, tlv: bytes, sample_rate: int, frame_samples: int) -> bytes:
        if not OPUS_AVAILABLE:
            raise RuntimeError("opuslib unavailable, cannot decode uplink opus")
        if self._decoder is None or self._decoder_rate != sample_rate:
            self._decoder = opuslib.Decoder(sample_rate, CHANNELS)
            self._decoder_rate = sample_rate
        pcm = bytearray()
        for pkt in unpack_opus_tlv(tlv):
            try:
                pcm += self._decoder.decode(pkt, frame_samples, decode_fec=False)  # type: ignore[union-attr]
            except Exception as e:
                print(f"⚠️ opus_decode failed (pkt={len(pkt)}B): {e}")
        return bytes(pcm)


class DownlinkOpusEncoder:
    """Encode downlink mono int16 PCM to Opus packets (40 ms frames, VOIP)."""

    def __init__(self) -> None:
        self._encoder = None
        self._pcm_residual = b""
        if OPUS_AVAILABLE:
            try:
                self._encoder = opuslib.Encoder(
                    DOWNLINK_SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP
                )
                self._encoder.bitrate = OPUS_BITRATE
            except Exception as e:
                print(f"⚠️ Opus encoder init failed, falling back to PCM: {e}")
                self._encoder = None

    @property
    def uses_opus(self) -> bool:
        return self._encoder is not None

    def encode_pcm(self, pcm: bytes) -> List[bytes]:
        if not self._encoder:
            return []
        buf = self._pcm_residual + pcm
        packets: List[bytes] = []
        i = 0
        while i + OPUS_FRAME_BYTES_PCM <= len(buf):
            frame = buf[i : i + OPUS_FRAME_BYTES_PCM]
            packets.append(self._encoder.encode(frame, OPUS_FRAME_SAMPLES))
            i += OPUS_FRAME_BYTES_PCM
        self._pcm_residual = buf[i:]
        return packets

    def flush_residual(self) -> List[bytes]:
        """Pad and encode any partial PCM frame; clears residual."""
        if not self._encoder or not self._pcm_residual:
            self._pcm_residual = b""
            return []
        pad = OPUS_FRAME_BYTES_PCM - len(self._pcm_residual)
        if pad > 0:
            padded = self._pcm_residual + b"\x00" * pad
        else:
            padded = self._pcm_residual[:OPUS_FRAME_BYTES_PCM]
        try:
            pkt = self._encoder.encode(padded, OPUS_FRAME_SAMPLES)
        except Exception as e:
            print(f"⚠️ Error flushing downlink Opus residual: {e}")
            self._pcm_residual = b""
            return []
        self._pcm_residual = b""
        return [pkt]

    def clear(self) -> None:
        self._pcm_residual = b""
