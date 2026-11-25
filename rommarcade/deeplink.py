"""Deep links: rommarcade://open?rom=…&platform=…&name=…&play=1

The pending target is persisted so a URL that arrives while the service is
starting still lands on the game after the UI loads.  `take` consumes it once,
which is what keeps a page reload from navigating again.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from typing import Any

from .config import data_dir

SCHEME = "rommarcade"


class DeepLinkError(ValueError):
    pass


def parse_deeplink(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise DeepLinkError("An empty deep link.")
    if text.lower().startswith(SCHEME + "://"):
        parsed = urllib.parse.urlsplit(text)
        action = parsed.netloc or parsed.path.strip("/")
        query = urllib.parse.parse_qs(parsed.query)
    elif text.lower().startswith(SCHEME + ":"):
        parsed = urllib.parse.urlsplit(text)
        action = parsed.path.strip("/")
        query = urllib.parse.parse_qs(parsed.query)
    else:
        raise DeepLinkError(f"A RomMArcade link starts with {SCHEME}://")
    action = (action or "").lower()
    if action not in ("open", "play"):
        raise DeepLinkError(f"Unknown RomMArcade action: {action or 'none'}")
    values = {key: v[0] for key, v in query.items() if v}
    try:
        rom_id = int(values.get("rom") or values.get("rom_id") or 0)
    except ValueError:
        raise DeepLinkError("The link has a ROM id that is not a number.") from None
    if rom_id <= 0:
        raise DeepLinkError("The link does not name a ROM id.")
    target = {
        "rom": rom_id,
        "platform": values.get("platform") or values.get("platform_slug") or "",
        "name": values.get("name") or values.get("rom_name") or "",
        "play": action == "play" or str(values.get("play") or "").lower() in ("1", "true", "yes"),
        "server": values.get("server") or "",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return target


class DeepLinkQueue:
    def __init__(self, path: str | None = None):
        self._path = path or os.path.join(data_dir(), "deeplink.json")
        self._lock = threading.RLock()
        self._target: dict[str, Any] | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("rom"):
                self._target = data
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        if self._target is None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
            return
        temp = self._path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self._target, handle, indent=2)
            handle.write("\n")
        os.replace(temp, self._path)

    def push(self, target: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._target = dict(target)
            self._save()
            return dict(self._target)

    def take(self) -> dict[str, Any] | None:
        with self._lock:
            target = self._target
            self._target = None
            self._save()
            return dict(target) if target else None

    def peek(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._target) if self._target else None
