#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _requirements_hash(requirements_path: Path) -> str:
    data = requirements_path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def _run(cmd: list[str], *, cwd: Path) -> int:
    return subprocess.call(cmd, cwd=str(cwd))


def _ensure_venv(root: Path) -> Path:
    venv_dir = root / ".venv"
    vpy = _venv_python(root)

    if vpy.exists():
        return vpy

    print("mock_nvr: creating .venv (first run) ...")
    venv_dir.mkdir(parents=True, exist_ok=True)
    builder = venv.EnvBuilder(with_pip=True, clear=False, upgrade=False)
    builder.create(str(venv_dir))

    if not vpy.exists():
        raise SystemExit(f"Expected venv python at {vpy} but it does not exist")
    return vpy


def _ensure_deps(root: Path, vpy: Path) -> None:
    req = root / "requirements.txt"
    if not req.exists():
        return

    marker_dir = root / ".venv"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / ".mock_nvr_requirements.sha256"

    wanted = _requirements_hash(req)
    current = marker.read_text().strip() if marker.exists() else ""
    if current == wanted:
        # Keep this quiet by default; most runs proceed straight into running the app.
        return

    print("mock_nvr: installing Python deps into .venv (may take a minute) ...")
    rc = _run(
        [
            str(vpy),
            "-m",
            "pip",
            "--disable-pip-version-check",
            "install",
            "-r",
            str(req),
        ],
        cwd=root,
    )
    if rc != 0:
        raise SystemExit(rc)
    marker.write_text(wanted)


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    os.chdir(str(root))
    vpy = _venv_python(root)

    # Opt-out for offline/controlled environments.
    if "--no-bootstrap" not in argv:
        vpy = _ensure_venv(root)
        _ensure_deps(root, vpy)
    else:
        argv = [a for a in argv if a != "--no-bootstrap"]

    # If we weren't launched from the venv, but the venv exists, re-exec into it.
    if vpy.exists() and Path(sys.executable).resolve() != vpy.resolve():
        os.execv(str(vpy), [str(vpy), "-m", "mock_nvr", *argv])

    # Fallback: run in current interpreter environment.
    # (This keeps it usable even if someone didn't create .venv yet.)
    os.execv(sys.executable, [sys.executable, "-m", "mock_nvr", *argv])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
