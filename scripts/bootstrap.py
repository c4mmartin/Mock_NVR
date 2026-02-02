#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _run(cmd: list[str], *, cwd: Path) -> None:
    subprocess.check_call(cmd, cwd=str(cwd))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap a local .venv for mock_nvr")
    parser.add_argument(
        "--venv-dir",
        default=".venv",
        help="Venv directory (default: .venv)",
    )
    parser.add_argument(
        "--editable",
        action="store_true",
        help="Install this project in editable mode (pip install -e .)",
    )
    parser.add_argument(
        "--requirements",
        action="store_true",
        help="Install from requirements.txt (default if --editable not set)",
    )
    parser.add_argument(
        "--upgrade-pip",
        action="store_true",
        help="Upgrade pip in the venv",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    venv_dir = root / args.venv_dir

    # Create venv if missing
    if not venv_dir.exists():
        import venv

        builder = venv.EnvBuilder(with_pip=True, clear=False, upgrade=False)
        builder.create(str(venv_dir))

    vpy = _venv_python(root)
    if not vpy.exists():
        raise SystemExit(f"Expected venv python at {vpy} but it does not exist")

    if args.upgrade_pip:
        _run([str(vpy), "-m", "pip", "install", "--upgrade", "pip"], cwd=root)

    use_editable = bool(args.editable)
    use_requirements = bool(args.requirements) or not use_editable

    if use_requirements:
        req = root / "requirements.txt"
        if not req.exists():
            raise SystemExit("requirements.txt not found")
        _run([str(vpy), "-m", "pip", "install", "-r", str(req)], cwd=root)

    if use_editable:
        _run([str(vpy), "-m", "pip", "install", "-e", "."], cwd=root)

    # Convenience hint
    launcher = root / "scripts" / "mock-nvr.py"
    if launcher.exists():
        print(f"OK: venv ready. Run: {vpy} -m mock_nvr --help")
        print(f"Or: python {launcher} --help")

    # Basic optional dependency hints
    if shutil.which("ffmpeg") is None:
        print("WARNING: ffmpeg not found on PATH (RTSP publishing will fail).")
        if os.name == "nt":
            print("  Install: winget install Gyan.FFmpeg")
        else:
            print("  Install (macOS): brew install ffmpeg")

    if shutil.which("mediamtx") is None:
        print("NOTE: mediamtx not found on PATH (RTSP server auto-start may be disabled).")
        if os.name == "nt":
            print("  Install: winget search mediamtx (or download from MediaMTX releases)")
        else:
            print("  Install (macOS): brew install mediamtx")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
