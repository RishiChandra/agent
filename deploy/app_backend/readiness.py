"""Offline audio checks, plus optional read-only database connectivity.

Run once for release validation, not as the frequent container health check.
No model downloads, provider requests, schema changes or application writes.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))


async def audio_checks():
    import opuslib
    import nltk
    nltk.data.find("tokenizers/punkt_tab/english/")
    from vosk import KaldiRecognizer
    from developer_ws.stt import _load_model_sync
    from developer_ws.tts import _load_voice, synthesize_speech_pcm24
    from developer_ws.vad import SileroVAD

    pcm = bytes(320 * 2)
    encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
    packet = encoder.encode(pcm, 320)
    decoded = opuslib.Decoder(16000, 1).decode(packet, 320)
    if len(decoded) != len(pcm):
        raise RuntimeError("Opus round-trip length mismatch")
    model = _load_model_sync()
    if model is None:
        raise RuntimeError("Vosk model unavailable")
    recognizer = KaldiRecognizer(model, 16000)
    recognizer.AcceptWaveform(bytes(16000 * 2))
    json.loads(recognizer.FinalResult())
    voice_path = Path(os.environ["PIPER_MODEL_PATH"])
    if not Path(str(voice_path) + ".json").is_file() or _load_voice() is None:
        raise RuntimeError("Piper model or companion configuration unavailable")
    if not await synthesize_speech_pcm24("Backend audio readiness check."):
        raise RuntimeError("Piper produced no audio")
    vad = SileroVAD()
    await vad.process(bytes(512 * 2))


def database_check():
    import psycopg2

    with psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], connect_timeout=5,
    ) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise RuntimeError("Database readiness failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="store_true", help="Also verify SELECT 1 using DB_* and PG* settings")
    args = parser.parse_args()
    try:
        asyncio.run(audio_checks())
        if args.database:
            database_check()
    except Exception as error:
        # Avoid emitting DSNs, credentials or application records.
        print(json.dumps({"ready": False, "error_type": type(error).__name__}))
        return 1
    print(json.dumps({"ready": True, "audio": True, "database": "passed" if args.database else "not checked",
                      "revision": os.environ.get("APP_REVISION", "unknown"),
                      "source_tree_sha256": os.environ.get("APP_SOURCE_TREE_SHA256", "unknown")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
