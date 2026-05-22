"""Download the Piper TTS voice for local development.

The voice file (~63 MB .onnx + ~5 KB .onnx.json) lives in ``piper_voices/`` at the
repo root, matching the default in ``app/developer_ws/tts.py``. It's not committed
to git — same pattern as ``vosk-model-small-en-us-0.15/`` — so a fresh checkout
needs to fetch it once. Without the voice, Piper logs ``"piper voice unavailable;
TTS disabled"`` at runtime and the assistant's TTS replies are silent.

On Azure App Service, the voice lives on the persistent ``/home/data/`` volume
(see ``azure-deploy.sh``) and is pointed at via the ``PIPER_MODEL_PATH`` env var —
this script is local-only.

Run from anywhere:

    python scripts/setup_piper_voice.py

Override the voice with --voice (e.g. ``en_US-lessac-medium``) or skip the download
if the file already exists.
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

# Layout on huggingface.co/rhasspy/piper-voices: en/en_US/<speaker>/<quality>/...
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Default matches app/developer_ws/tts.py:_DEFAULT_MODEL.
DEFAULT_VOICE = "en_US-amy-medium"

REPO_ROOT = Path(__file__).resolve().parent.parent
VOICES_DIR = REPO_ROOT / "piper_voices"


def _voice_url(voice: str) -> tuple[str, str]:
    """Resolve `en_US-amy-medium` -> URLs for the .onnx and .onnx.json files.

    Voice id convention: `<lang>_<region>-<speaker>-<quality>`. The HF repo
    layout is `<lang>/<lang>_<region>/<speaker>/<quality>/<voice>.onnx[.json]`.
    """
    try:
        lang_region, speaker, quality = voice.split("-", 2)
        lang = lang_region.split("_", 1)[0]
    except ValueError:
        sys.exit(
            f"Could not parse voice id '{voice}'. Expected '<lang>_<region>-<speaker>-<quality>', "
            f"e.g. en_US-amy-medium."
        )
    base = f"{HF_BASE}/{lang}/{lang_region}/{speaker}/{quality}"
    return f"{base}/{voice}.onnx", f"{base}/{voice}.onnx.json"


def _download(url: str, dest: Path) -> None:
    """Stream-download `url` to `dest`. Atomic via a temp file + rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  {url}\n  -> {dest}")
    with urllib.request.urlopen(url) as resp, tmp.open("wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        chunk = 1024 * 64
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            out.write(buf)
            read += len(buf)
            if total:
                pct = 100 * read / total
                sys.stdout.write(f"\r  {read / 1_000_000:>6.1f} MB / {total / 1_000_000:.1f} MB ({pct:5.1f}%)")
                sys.stdout.flush()
        if total:
            sys.stdout.write("\n")
    tmp.replace(dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"Piper voice id (default: {DEFAULT_VOICE}). See huggingface.co/rhasspy/piper-voices.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the files already exist.",
    )
    args = parser.parse_args()

    onnx_url, json_url = _voice_url(args.voice)
    onnx_path = VOICES_DIR / f"{args.voice}.onnx"
    json_path = VOICES_DIR / f"{args.voice}.onnx.json"

    if onnx_path.is_file() and json_path.is_file() and not args.force:
        print(f"Already present: {onnx_path}")
        print("Pass --force to re-download.")
        return 0

    print(f"Downloading voice {args.voice} -> {VOICES_DIR}")
    try:
        _download(onnx_url, onnx_path)
        _download(json_url, json_path)
    except Exception as e:
        print(f"\nDownload failed: {e}")
        print(
            "Check the voice id at https://huggingface.co/rhasspy/piper-voices,\n"
            "or download manually and place the two files in piper_voices/."
        )
        return 1

    print(f"\nVoice ready: {onnx_path}")
    if args.voice != DEFAULT_VOICE:
        print(
            f"\nThis is a non-default voice. Set in your .env (override the repo-root default):\n"
            f"  PIPER_MODEL_PATH=piper_voices/{args.voice}.onnx"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
