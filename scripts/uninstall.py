#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Uninstall/cleanup local mock_nvr environment")
    parser.add_argument(
        "--remove-venv",
        action="store_true",
        help="Remove .venv (default: true)",
    )
    parser.add_argument(
        "--keep-venv",
        action="store_true",
        help="Do not remove .venv",
    )
    parser.add_argument(
        "--remove-logs",
        action="store_true",
        help="Remove logs/ directory",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent

    remove_venv = args.remove_venv or not args.keep_venv

    targets: list[Path] = []
    if remove_venv:
        targets.append(root / ".venv")
    if args.remove_logs:
        targets.append(root / "logs")

    if not targets:
        print("Nothing to do.")
        return 0

    print("Will remove:")
    for t in targets:
        print(f"- {t}")

    if not args.yes:
        resp = input("Proceed? [y/N]: ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 1

    for t in targets:
        if t.exists():
            shutil.rmtree(t, ignore_errors=True)

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
