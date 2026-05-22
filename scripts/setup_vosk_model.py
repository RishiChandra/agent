"""Download the Vosk STT model for local development.

The model directory (~70 MB unpacked) lives at the repo root, matching the path
``VOSK_MODEL_PATH`` resolves to in ``app/developer_ws/stt.py``. It's not committed
to git — same pattern as ``piper_voices/`` — so a fresh checkout needs to fetch it
once. Without the model, STT returns "" and the assistant never hears anything.

On Azure App Service, the model lives on the persistent ``/home/data/`` volume
(see ``azure-deploy.sh``) and is pointed at via the ``VOSK_MODEL_PATH`` env var —
this script is local-only.

Run from anywhere:

    python scripts/setup_vosk_model.py

Override the model with --model (e.g. ``vosk-model-en-us-0.22`` for the large one)
or skip the download if it's already on disk.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

# Alphacephei mirrors all Vosk models at this base URL.
VOSK_BASE = "https://alphacephei.com/vosk/models"

# Default matches the path checked by azure-deploy.sh preflight and the
# README quickstart.
DEFAULT_MODEL = "vosk-model-small-en-us-0.15"

REPO_ROOT = Path(__file__).resolve().parent.parent

# After extraction, this file is the canonical "model is real" check — same
# sentinel used by azure-deploy.sh's preflight.
SENTINEL = Path("am") / "final.mdl"


def _model_url(model: str) -> str:
    return f"{VOSK_BASE}/{model}.zip"


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
                sys.stdout.write(
                    f"\r  {read / 1_000_000:>6.1f} MB / {total / 1_000_000:.1f} MB ({pct:5.1f}%)"
                )
                sys.stdout.flush()
        if total:
            sys.stdout.write("\n")
    tmp.replace(dest)


def _extract(zip_path: Path, dest_root: Path, model: str) -> Path:
    """Unzip `zip_path` into `dest_root`. Returns the extracted model dir.

    Vosk zips wrap their content in a single top-level directory matching the
    model name. We extract directly to repo root and rely on that wrapping.
    """
    with zipfile.ZipFile(zip_path) as zf:
        # Defend against zips that don't wrap their contents in the expected
        # top-level directory — we want the final layout to match what
        # VOSK_MODEL_PATH expects.
        names = zf.namelist()
        if not names:
            sys.exit(f"Empty zip: {zip_path}")
        top = names[0].split("/", 1)[0]
        if top != model:
            print(
                f"  Note: zip's top-level dir is '{top}', not '{model}'. "
                "Extracting and renaming."
            )
        zf.extractall(dest_root)
    extracted = dest_root / top
    target = dest_root / model
    if extracted != target:
        if target.exists():
            shutil.rmtree(target)
        extracted.rename(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Vosk model name (default: {DEFAULT_MODEL}). "
        f"See https://alphacephei.com/vosk/models for the catalog.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download and re-extract even if the model is already on disk.",
    )
    args = parser.parse_args()

    model_dir = REPO_ROOT / args.model
    sentinel = model_dir / SENTINEL

    if sentinel.is_file() and not args.force:
        print(f"Already present: {model_dir}")
        print(f"  Sentinel found: {sentinel}")
        print("Pass --force to re-download.")
        return 0

    if model_dir.exists() and args.force:
        print(f"Removing existing {model_dir} ...")
        shutil.rmtree(model_dir)

    url = _model_url(args.model)
    print(f"Downloading Vosk model {args.model} -> {REPO_ROOT}")

    with tempfile.NamedTemporaryFile(
        suffix=".zip", delete=False, dir=str(REPO_ROOT)
    ) as tmp:
        zip_path = Path(tmp.name)

    try:
        _download(url, zip_path)
        print("Extracting ...")
        extracted = _extract(zip_path, REPO_ROOT, args.model)
    except Exception as e:
        print(f"\nSetup failed: {e}")
        print(
            "Check the model name at https://alphacephei.com/vosk/models,\n"
            "or download manually and unpack the zip at the repo root."
        )
        return 1
    finally:
        try:
            zip_path.unlink()
        except OSError:
            pass

    if not sentinel.is_file():
        print(
            f"\nDownloaded but sentinel missing: {sentinel}\n"
            f"Check {extracted} layout — Vosk zip structure may have changed."
        )
        return 1

    print(f"\nModel ready: {extracted}")
    if args.model != DEFAULT_MODEL:
        print(
            f"\nThis is a non-default model. Set in your .env (override the repo-root default):\n"
            f"  VOSK_MODEL_PATH={args.model}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
