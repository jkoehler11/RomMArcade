"""Local installs and play history, kept in one atomically written JSON file."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from .config import data_dir, state_path


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class StateStore:
    def __init__(self, path: str | None = None):
        self._path = path or state_path()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"installs": {}, "history": []}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._data["installs"] = data.get("installs") or {}
                self._data["history"] = data.get("history") or []
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        temp = self._path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp, self._path)

    # -- installs ---------------------------------------------------------
    def record_install(self, rom: dict[str, Any], path: str, size: int, platform: str) -> dict[str, Any]:
        with self._lock:
            entry = {
                "rom_id": int(rom.get("id")),
                "name": rom.get("name") or "",
                "platform": platform,
                "platform_slug": rom.get("platform_slug") or rom.get("platform_fs_slug") or "",
                "path": path,
                "size": int(size or 0),
                "has_multiple_files": bool(rom.get("has_multiple_files")),
                "cover": rom.get("path_cover_large") or rom.get("path_cover_small") or "",
                "installed_at": _now(),
            }
            self._data["installs"][str(entry["rom_id"])] = entry
            self._save()
            return entry

    def install_for(self, rom_id: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data["installs"].get(str(rom_id))
            if not entry:
                return None
            if not os.path.exists(entry.get("path", "")):
                return None
            return dict(entry)

    def installs(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = [dict(e) for e in self._data["installs"].values()]
        live = [e for e in entries if os.path.exists(e.get("path", ""))]
        live.sort(key=lambda e: e.get("installed_at") or "", reverse=True)
        return live

    def forget(self, rom_id: int) -> None:
        with self._lock:
            self._data["installs"].pop(str(rom_id), None)
            self._save()

    def repair(self) -> int:
        """Drop entries whose files were moved or deleted."""
        with self._lock:
            before = len(self._data["installs"])
            self._data["installs"] = {
                key: value
                for key, value in self._data["installs"].items()
                if os.path.exists(value.get("path", ""))
            }
            removed = before - len(self._data["installs"])
            if removed:
                self._save()
            return removed

    # -- history ----------------------------------------------------------
    def mark_played(self, rom: dict[str, Any], platform: str) -> None:
        with self._lock:
            entry = {
                "rom_id": int(rom.get("id")),
                "name": rom.get("name") or "",
                "platform": platform,
                "played_at": _now(),
            }
            history = [h for h in self._data["history"] if h.get("rom_id") != entry["rom_id"]]
            history.insert(0, entry)
            self._data["history"] = history[:100]
            self._save()

    def history(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(h) for h in self._data["history"][:limit]]

    def snapshot(self) -> dict[str, Any]:
        return {"installs": self.installs(), "history": self.history()}


def ensure_dirs() -> None:
    for path in (data_dir(),):
        os.makedirs(path, exist_ok=True)
