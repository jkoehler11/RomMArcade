#!/usr/bin/env python3
"""Build the standalone RomMArcade desktop app for the current operating system."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller is missing. Install the build extra:", file=sys.stderr)
        print("  python3 -m pip install \".[build]\"", file=sys.stderr)
        return 2
    if importlib.util.find_spec("webview") is None:
        print("pywebview is missing. Install the desktop extra:", file=sys.stderr)
        print("  python3 -m pip install \".[desktop]\"", file=sys.stderr)
        return 2
    spec = os.path.join(root, "packaging", "RomMArcade.spec")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", spec]
    print(" ".join(command))
    result = subprocess.run(command, cwd=root)
    if result.returncode:
        return result.returncode
    if sys.platform == "darwin":
        print("Built dist/RomMArcade.app")
    elif sys.platform == "win32":
        print("Built dist/RomMArcade/RomMArcade.exe")
    else:
        print("Built dist/RomMArcade/RomMArcade")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
