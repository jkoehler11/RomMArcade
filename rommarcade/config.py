"""Configuration, paths and server-profile handling for RomMArcade.

The config is a single JSON document written atomically.  Secrets (tokens and
passwords) live in that file with 0600 permissions and are never returned to
the browser unmasked.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORT = 8790
CONFIG_VERSION = 1


class ConfigError(ValueError):
    """A configuration value the user can fix."""


def _xdg(env: str, fallback: str) -> str:
    value = os.environ.get(env, "").strip()
    return value or fallback


def config_dir() -> str:
    override = os.environ.get("ROMARCADE_HOME", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(_xdg("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "rommarcade")


def data_dir() -> str:
    override = os.environ.get("ROMARCADE_HOME", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(_xdg("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "rommarcade")


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def state_path() -> str:
    return os.path.join(data_dir(), "state.json")


def cache_dir() -> str:
    return os.path.join(data_dir(), "cache")


def log_path() -> str:
    return os.path.join(data_dir(), "rommarcade.log")


def normalize_url(raw: str) -> str:
    """Turn anything a person might paste into a RomM base URL."""
    text = (raw or "").strip()
    if not text:
        raise ConfigError("A RomM server URL is required.")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.IGNORECASE):
        host_part = text.split("/", 1)[0]
        local = (
            host_part.startswith(("localhost", "127.", "[::1]"))
            or re.match(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$", host_part) is not None
            or host_part.endswith(".local")
        )
        text = ("http://" if local else "https://") + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise ConfigError("The server URL must start with http:// or https://.")
    if not parts.netloc:
        raise ConfigError("The server URL is missing a host name.")
    path = parts.path.rstrip("/")
    if path.lower().endswith("/api"):
        path = path[:-4].rstrip("/")
    clean = urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, ""))
    return clean.rstrip("/")


def validate_auth(auth: Any) -> dict[str, Any]:
    if auth is None:
        return {"mode": "none"}
    if not isinstance(auth, dict):
        raise ConfigError("Authentication must be an object.")
    mode = str(auth.get("mode") or "none").strip().lower()
    if mode in ("token", "bearer", "api", "api-token"):
        token = str(auth.get("token") or "").strip()
        if not token:
            raise ConfigError("A client token is required.")
        return {"mode": "token", "token": token}
    if mode in ("password", "basic"):
        username = str(auth.get("username") or "").strip()
        password = str(auth.get("password") or "")
        if not username:
            raise ConfigError("A username is required.")
        if not password:
            raise ConfigError("A password is required.")
        return {"mode": "password", "username": username, "password": password}
    if mode == "none":
        return {"mode": "none"}
    raise ConfigError(f"Unsupported authentication mode: {mode}")


def auth_headers(auth: dict[str, Any]) -> dict[str, str]:
    mode = (auth or {}).get("mode", "none")
    if mode == "token":
        return {"Authorization": f"Bearer {auth['token']}"}
    if mode == "password":
        raw = f"{auth['username']}:{auth['password']}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    return {}


def new_server_id() -> str:
    return uuid.uuid4().hex[:12]


def default_config() -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "active_server": "",
        "servers": [],
        "web": {"port": DEFAULT_PORT, "bind": "0.0.0.0"},
        "emudeck": {
            "enabled": True,
            "root": "",
            "roms_root": "",
            "bios_root": "",
            "launchers_root": "",
            "overrides": {},
        },
        "ui": {"theme": "arcade", "sort": "name", "view": "grid"},
        "launch": {"detach": True, "keep_open": False},
        "downloads": {"concurrency": 2},
    }


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class ConfigStore:
    """Thread-safe JSON configuration store."""

    def __init__(self, path: str | None = None):
        self._path = path or config_path()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.load()

    # -- disk -------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        with self._lock:
            data = default_config()
            if os.path.exists(self._path):
                try:
                    with open(self._path, "r", encoding="utf-8") as handle:
                        stored = json.load(handle)
                    if isinstance(stored, dict):
                        data = _merge(data, stored)
                except (OSError, json.JSONDecodeError):
                    # A corrupt file must not brick the app; keep a copy.
                    try:
                        os.replace(self._path, self._path + ".broken")
                    except OSError:
                        pass
            self._data = data
            return copy.deepcopy(self._data)

    def save(self) -> None:
        with self._lock:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            os.makedirs(data_dir(), exist_ok=True)
            temp = self._path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)

    # -- reads ------------------------------------------------------------
    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def active_server(self) -> dict[str, Any] | None:
        with self._lock:
            active = self._data.get("active_server") or ""
            for server in self._data.get("servers", []):
                if server.get("id") == active:
                    return copy.deepcopy(server)
            return None

    def masked(self) -> dict[str, Any]:
        """The config as the browser may see it."""
        with self._lock:
            data = copy.deepcopy(self._data)
        for server in data.get("servers", []):
            auth = server.get("auth") or {}
            if auth.get("mode") == "token":
                auth["token"] = _mask(auth.get("token", ""))
                auth["has_secret"] = bool((self._server_by_id(server.get("id")) or {}).get("auth", {}).get("token"))
            elif auth.get("mode") == "password":
                auth["password"] = _mask(auth.get("password", ""))
                auth["has_secret"] = bool((self._server_by_id(server.get("id")) or {}).get("auth", {}).get("password"))
            else:
                auth["has_secret"] = False
        return data

    def _server_by_id(self, server_id: str | None) -> dict[str, Any] | None:
        for server in self._data.get("servers", []):
            if server.get("id") == server_id:
                return server
        return None

    # -- writes -----------------------------------------------------------
    def add_server(
        self,
        url: str,
        auth: dict[str, Any] | None = None,
        name: str = "",
        make_active: bool = True,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        clean_url = normalize_url(url)
        clean_auth = validate_auth(auth)
        with self._lock:
            existing = None
            for server in self._data["servers"]:
                if server.get("id") == server_id or normalize_url(server.get("url", "")) == clean_url:
                    existing = server
                    break
            if existing is None:
                existing = {"id": server_id or new_server_id()}
                self._data["servers"].append(existing)
            existing["url"] = clean_url
            existing["auth"] = clean_auth
            existing["name"] = (name or existing.get("name") or _pretty_host(clean_url)).strip()
            existing.setdefault("added_at", _now())
            if make_active or not self._data.get("active_server"):
                self._data["active_server"] = existing["id"]
            self.save()
            return copy.deepcopy(existing)

    def update_server(self, server_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            server = self._server_by_id(server_id)
            if server is None:
                raise ConfigError("No such server profile.")
            if "url" in patch and patch["url"] is not None:
                server["url"] = normalize_url(str(patch["url"]))
            if "name" in patch and patch["name"] is not None:
                server["name"] = str(patch["name"]).strip() or _pretty_host(server["url"])
            if patch.get("auth") is not None:
                server["auth"] = validate_auth(patch["auth"])
            if "tls_verify" in patch:
                server["tls_verify"] = bool(patch["tls_verify"])
            self.save()
            return copy.deepcopy(server)

    def remove_server(self, server_id: str) -> None:
        with self._lock:
            self._data["servers"] = [s for s in self._data["servers"] if s.get("id") != server_id]
            if self._data.get("active_server") == server_id:
                self._data["active_server"] = self._data["servers"][0]["id"] if self._data["servers"] else ""
            self.save()

    def activate_server(self, server_id: str) -> dict[str, Any]:
        with self._lock:
            server = self._server_by_id(server_id)
            if server is None:
                raise ConfigError("No such server profile.")
            self._data["active_server"] = server_id
            self.save()
            return copy.deepcopy(server)

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            allowed = {"web", "emudeck", "ui", "launch", "downloads"}
            for key, value in (patch or {}).items():
                if key in allowed and isinstance(value, dict):
                    self._data[key] = _merge(self._data.get(key, {}), value)
            self.save()
            return self.get()

    def set_override(self, platform_key: str, override: dict[str, Any] | None) -> None:
        with self._lock:
            overrides = self._data.setdefault("emudeck", {}).setdefault("overrides", {})
            if override is None:
                overrides.pop(platform_key, None)
            else:
                clean = {k: v for k, v in override.items() if v not in (None, "")}
                if clean:
                    overrides[platform_key] = clean
                else:
                    overrides.pop(platform_key, None)
            self.save()


def _mask(secret: str) -> str:
    if not secret:
        return ""
    return "•" * min(12, max(4, len(secret) // 3))


def _pretty_host(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc or url


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Paths:
    config: str
    state: str
    cache: str
    log: str
