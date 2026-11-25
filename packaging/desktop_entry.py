"""PyInstaller entry point for the RomMArcade desktop app."""

from __future__ import annotations

import sys

from rommarcade.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main(["app", *sys.argv[1:]]))
