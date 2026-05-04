"""
Download MinGW64 libopus from MSYS2 and install as opus.dll next to the venv Python.

opuslib uses ctypes.util.find_library("opus"), which on Windows only searches PATH.
Activating the venv prepends .venv\\Scripts to PATH, so opus.dll in Scripts is found.

Run from repo root (or anywhere) with the PROJECT venv's interpreter:

  .\\.venv\\Scripts\\python.exe scripts\\install_opus_windows.py

Requires: curl.exe OR urllib (stdlib), and tar.exe with zstd (Windows 10+ bsdtar).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Pinned MSYS2 mingw64 package (update if mirror 404s).
DEFAULT_PKG_URL = (
    "https://mirror.msys2.org/mingw/mingw64/"
    "mingw-w64-x86_64-opus-1.5.2-1-any.pkg.tar.zst"
)
SOURCE_DLL = Path("mingw64") / "bin" / "libopus-0.dll"
TARGET_NAME = "opus.dll"


def _venv_scripts_dir() -> Path:
    if sys.platform != "win32":
        sys.exit("This script only supports Windows.")
    parent = Path(sys.executable).resolve().parent
    if parent.name.lower() != "scripts":
        sys.exit(
            "Use the project virtualenv's Python so opus.dll lands in .venv\\Scripts.\n"
            "Example:\n"
            "  .\\.venv\\Scripts\\python.exe scripts\\install_opus_windows.py"
        )
    return parent


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "agent-repo-install-opus/1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _extract_pkg(archive: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Windows 10+ tar (bsdtar) supports zstd for .pkg.tar.zst
    subprocess.run(
        ["tar", "-xf", str(archive), "-C", str(out_dir)],
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Install opus.dll for opuslib (Windows).")
    parser.add_argument(
        "--url",
        default=DEFAULT_PKG_URL,
        help="MSYS2 mingw-w64-x86_64-opus .pkg.tar.zst URL",
    )
    args = parser.parse_args()

    scripts = _venv_scripts_dir()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "opus.pkg.tar.zst"
        extract_root = tmp_path / "extract"
        print(f"Downloading:\n  {args.url}")
        _download(args.url, archive)
        print(f"Extracting ({archive.stat().st_size} bytes) …")
        _extract_pkg(archive, extract_root)
        src_dll = extract_root / SOURCE_DLL
        if not src_dll.is_file():
            sys.exit(f"Expected DLL missing after extract: {src_dll}")

        dest = scripts / TARGET_NAME
        shutil.copy2(src_dll, dest)
        print(f"Installed:\n  {dest}")

    # Verify (Scripts must be on PATH as with venv activate)
    import os

    env = os.environ.copy()
    env["PATH"] = str(scripts) + os.pathsep + env.get("PATH", "")
    print("Verifying import with PATH including Scripts …")
    r = subprocess.run(
        [sys.executable, "-c", "import ctypes.util; import opuslib; print(ctypes.util.find_library('opus')); print('opuslib: ok')"],
        env=env,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stderr or r.stdout)
        sys.exit("Verification failed.")
    print(r.stdout.strip())


if __name__ == "__main__":
    main()
