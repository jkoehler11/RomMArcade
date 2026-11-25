"""The local RomMArcade service: JSON API, static UI, downloads and launching."""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import re
import socket
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import CLIENT_ID, PRODUCT, PRODUCT_TAGLINE, REQUESTED_SCOPES, USER_AGENT, __version__
from .config import ConfigError, ConfigStore, cache_dir, config_dir, data_dir, log_path
from .deeplink import DeepLinkError, DeepLinkQueue, parse_deeplink
from .downloads import DownloadManager
from .emudeck import (
    EmuDeckError,
    bios_target,
    build_command,
    detect as detect_emudeck,
    install_path,
    invalidate_cache as invalidate_emudeck,
    platform_mapping,
    rom_file,
    rom_target_name,
    spawn,
)
from .romm import RomMClient, RomMError
from .state import StateStore, ensure_dirs

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 2 * 1024 * 1024
COVER_LIMIT = 12 * 1024 * 1024


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _now() -> float:
    return time.time()


class HttpError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        out = {"error": self.message}
        out.update(self.extra)
        return out


class App:
    def __init__(self, config: ConfigStore | None = None):
        ensure_dirs()
        self.config = config or ConfigStore()
        self.state = StateStore()
        self.deeplinks = DeepLinkQueue()
        self.log_file = log_path()
        cfg = self.config.get()
        self.downloads = DownloadManager(
            self.state, self.log_file, int(cfg.get("downloads", {}).get("concurrency", 2))
        )
        self.started = _now()
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._pairings: dict[str, dict[str, Any]] = {}
        self._device_identifier = self._load_device_identifier()
        self._reachability: tuple[float, dict[str, Any]] | None = None

    # -- helpers ----------------------------------------------------------
    def log(self, message: str) -> None:
        try:
            os.makedirs(os.path.dirname(self.log_file) or ".", exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def _load_device_identifier(self) -> str:
        path = os.path.join(data_dir(), "device_identifier")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
            if value:
                return value
        except OSError:
            pass
        value = "rommarcade-" + uuid.uuid4().hex
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value

    def _cache_get(self, key: str, ttl: float):
        with self._lock:
            entry = self._cache.get(key)
        if entry and _now() - entry[0] <= ttl:
            return entry[1]
        return None

    def _cache_set(self, key: str, value: Any) -> Any:
        with self._lock:
            self._cache[key] = (_now(), value)
        return value

    def _cache_clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._reachability = None

    def client(self, server: dict[str, Any] | None = None, url: str | None = None, auth: dict[str, Any] | None = None, timeout: float = 25.0) -> RomMClient:
        if server is None:
            server = self.config.active_server()
        if server:
            return RomMClient(
                url or server.get("url", ""),
                auth if auth is not None else server.get("auth") or {"mode": "none"},
                tls_verify=bool(server.get("tls_verify", True)),
                timeout=timeout,
            )
        if url:
            return RomMClient(url, auth or {"mode": "none"}, timeout=timeout)
        raise HttpError(409, "No RomM server is configured yet.")

    def active_server(self) -> dict[str, Any] | None:
        return self.config.active_server()

    # -- RomM data --------------------------------------------------------
    def platforms(self) -> list[dict[str, Any]]:
        cached = self._cache_get("platforms", 90)
        if cached is not None:
            return cached
        server = self.active_server()
        if not server:
            return []
        raw = self.client(server).platforms()
        cfg = self.config.get()
        emu_cfg = cfg.get("emudeck", {})
        emu = detect_emudeck(emu_cfg)
        overrides = emu_cfg.get("overrides", {})
        installs = self.state.installs()
        counts: dict[str, int] = {}
        for entry in installs:
            counts[str(entry.get("platform") or "")] = counts.get(str(entry.get("platform") or ""), 0) + 1
        out = []
        for platform in raw:
            mapping = platform_mapping(platform, emu, emu["launchers"], emu["cores"], overrides)
            item = dict(platform)
            item["logo_url"] = f"/api/platform-logo/{platform.get('id')}" if platform.get("url_logo") else ""
            item["local"] = {
                **mapping,
                "installed_count": counts.get(mapping["platform"], 0),
            }
            out.append(item)
        out.sort(key=lambda p: (str(p.get("display_name") or p.get("name") or "").lower()))
        return self._cache_set("platforms", out)

    def platform_index(self) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for platform in self.platforms():
            for key in (
                str(platform.get("id") or ""),
                str(platform.get("slug") or "").lower(),
                str(platform.get("fs_slug") or "").lower(),
            ):
                if key:
                    index[key] = platform
        return index

    def resolve_platform(self, key: str | int | None) -> dict[str, Any]:
        index = self.platform_index()
        if key is None:
            raise HttpError(400, "A platform is required.")
        found = index.get(str(key)) or index.get(str(key).lower())
        if not found:
            raise HttpError(404, "No such platform in this RomM library.")
        return found

    def emudeck_snapshot(self) -> dict[str, Any]:
        cfg = self.config.get()
        emu_cfg = cfg.get("emudeck", {})
        emu = detect_emudeck(emu_cfg)
        platforms = []
        if self.active_server():
            try:
                for platform in self.platforms():
                    platforms.append(platform["local"])
            except (RomMError, HttpError):
                pass
        emu = dict(emu)
        emu["platforms"] = platforms
        emu["overrides"] = emu_cfg.get("overrides", {})
        return emu

    def rom_detail(self, rom_id: int, use_cache: bool = True) -> dict[str, Any]:
        key = f"rom:{self.config.get().get('active_server')}:{int(rom_id)}"
        if use_cache:
            cached = self._cache_get(key, 120)
            if cached is not None:
                return cached
        rom = self.client().rom(int(rom_id))
        if not rom.get("files"):
            data = self.client().roms(rom_ids=[int(rom_id)], with_files=True, limit=1)
            items = data.get("items") or []
            if items:
                rom = items[0]
        enriched = self.enrich_rom(rom, detail=True)
        return self._cache_set(key, enriched)

    def enrich_rom(self, rom: dict[str, Any], detail: bool = False) -> dict[str, Any]:
        fs_slug = str(rom.get("platform_fs_slug") or rom.get("platform_slug") or "")
        mapping: dict[str, Any] = {}
        try:
            platform = self.resolve_platform(fs_slug or rom.get("platform_id"))
            mapping = platform.get("local") or {}
        except HttpError:
            mapping = {}
        installed = self.state.install_for(int(rom.get("id"))) if rom.get("id") else None
        target = ""
        try:
            if mapping:
                target = install_path(rom, mapping)
        except (EmuDeckError, Exception):
            target = ""
        cover = rom.get("path_cover_large") or rom.get("url_cover") or ""
        out = {
            "id": rom.get("id"),
            "name": rom.get("name") or rom.get("fs_name") or "",
            "platform_id": rom.get("platform_id"),
            "platform_slug": rom.get("platform_slug"),
            "platform_fs_slug": rom.get("platform_fs_slug") or fs_slug,
            "platform_name": rom.get("platform_display_name") or (mapping or {}).get("name") or "",
            "size": int(rom.get("fs_size_bytes") or 0),
            "has_multiple_files": bool(rom.get("has_multiple_files")),
            "cover": cover,
            "cover_small": rom.get("path_cover_small") or "",
            "file_name": (rom_file(rom) or {}).get("file_name") or rom.get("fs_name") or "",
            "target": target,
            "installed": installed,
            "mapping": {
                "platform": mapping.get("platform") or fs_slug,
                "dir": mapping.get("dir") or "",
                "path": mapping.get("path") or "",
                "exists": bool(mapping.get("exists")),
                "launcher": mapping.get("launcher") or "",
                "launcher_name": mapping.get("launcher_name") or "",
                "launcher_path": mapping.get("launcher_path") or "",
                "core": mapping.get("core") or "",
                "ready": bool(mapping.get("exists") and mapping.get("launcher")),
            },
        }
        if detail:
            metadatum = rom.get("metadatum") if isinstance(rom.get("metadatum"), dict) else {}
            screenshots = []
            for shot in rom.get("merged_screenshots") or []:
                path = shot.get("path") if isinstance(shot, dict) else shot
                if isinstance(path, str) and path:
                    screenshots.append(path)
            out.update(
                {
                    "summary": rom.get("summary") or "",
                    "genres": metadatum.get("genres") or [],
                    "players": metadatum.get("player_count") or "",
                    "release_year": _release_year(metadatum.get("first_release_date")),
                    "screenshots": screenshots,
                    "files": [
                        {
                            "file_name": f.get("file_name"),
                            "size": f.get("file_size_bytes"),
                            "is_top_level": f.get("is_top_level"),
                        }
                        for f in rom.get("files") or []
                    ],
                    "target_name": rom_target_name(rom),
                }
            )
        return out

    def roms(self, platform_key: str | None, search: str, limit: int, offset: int, sort: str) -> dict[str, Any]:
        platform_ids = []
        if platform_key and platform_key not in ("all", "*"):
            platform_ids = [int(self.resolve_platform(platform_key).get("id"))]
        order_by, order_dir = _sort_params(sort)
        data = self.client().roms(
            platform_ids=platform_ids,
            search=search or None,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir,
        )
        items = [self.enrich_rom(rom) for rom in data.get("items") or []]
        return {
            "items": items,
            "total": int(data.get("total") or len(items)),
            "limit": int(data.get("limit") or limit),
            "offset": int(data.get("offset") or offset),
        }

    def firmware(self, platform_id: int) -> list[dict[str, Any]]:
        try:
            platform = self.resolve_platform(platform_id)
        except HttpError:
            platform = {"id": platform_id, "fs_slug": "", "name": ""}
        try:
            items = self.client().firmware(int(platform_id))
        except RomMError:
            items = []
        cfg = self.config.get()
        emu = detect_emudeck(cfg.get("emudeck", {}))
        out = []
        for item in items:
            target = bios_target(item, emu["bios_root"]) if emu["bios_root"] else ""
            out.append(
                {
                    **item,
                    "target": target,
                    "installed": bool(target and os.path.exists(target)),
                    "platform": platform.get("fs_slug") or platform.get("slug") or "",
                }
            )
        return out

    # -- reachability -----------------------------------------------------
    def server_status(self, fresh: bool = False) -> dict[str, Any]:
        server = self.active_server()
        if not server:
            return {"configured": False}
        if not fresh and self._reachability and _now() - self._reachability[0] < 20:
            return self._reachability[1]
        info: dict[str, Any] = {
            "configured": True,
            "id": server.get("id"),
            "name": server.get("name") or "",
            "url": server.get("url"),
            "reachable": False,
            "version": "",
            "user": None,
            "platforms": None,
            "error": "",
        }
        try:
            probe = self.client(server, timeout=8.0).probe()
            info.update(
                {
                    "reachable": True,
                    "version": probe.get("version") or "",
                    "user": probe.get("user"),
                    "platforms": probe.get("platforms"),
                }
            )
        except RomMError as exc:
            info["error"] = str(exc)
        self._reachability = (_now(), info)
        return info

    # -- dispatch ---------------------------------------------------------
    def dispatch(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, dict[str, str], Any]:
        try:
            if path.startswith("/api/"):
                return self._api(method, path, query, body)
            return self._static(method, path)
        except HttpError as exc:
            return exc.status, {"Content-Type": "application/json"}, exc.payload()
        except ConfigError as exc:
            return 400, {"Content-Type": "application/json"}, {"error": str(exc)}
        except EmuDeckError as exc:
            return 409, {"Content-Type": "application/json"}, {"error": str(exc)}
        except RomMError as exc:
            status = 401 if exc.auth_failed else 502
            return status, {"Content-Type": "application/json"}, {"error": str(exc), "romm_status": exc.status}
        except DeepLinkError as exc:
            return 400, {"Content-Type": "application/json"}, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the UI needs a JSON error, not a traceback page
            self.log("unhandled: " + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            return 500, {"Content-Type": "application/json"}, {"error": f"RomMArcade hit an internal error: {exc}"}

    def _api(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, dict[str, str], Any]:
        route = (method.upper(), path.rstrip("/") or "/")

        if route == ("GET", "/api/status"):
            cfg = self.config.get()
            emu = detect_emudeck(cfg.get("emudeck", {}))
            return 200, JSON, {
                "product": PRODUCT,
                "version": __version__,
                "tagline": PRODUCT_TAGLINE,
                "configured": bool(self.active_server()),
                "server": self.server_status(),
                "emudeck": {
                    "detected": bool(emu["detected"]),
                    "root": emu["root"],
                    "launchers": len([l for l in emu["launchers"] if l.get("ready")]),
                    "platforms": len(emu["platforms"]) if "platforms" in emu else 0,
                },
                "web_port": int(cfg.get("web", {}).get("port") or 8790),
                "device_identifier": self._device_identifier,
                "started": self.started,
                "downloads": len([j for j in self.downloads.list() if j["status"] in ("queued", "downloading", "extracting")]),
            }

        if route == ("GET", "/api/config"):
            return 200, JSON, {"config": self.config.masked(), "status": self.server_status()}

        if route == ("PUT", "/api/config"):
            return 200, JSON, self._apply_config(body or {})

        if route == ("POST", "/api/config/test"):
            return 200, JSON, self._test_server(body or {})

        if route == ("POST", "/api/config/pair/start"):
            return 200, JSON, self._pair_start(body or {})

        if route == ("POST", "/api/config/pair/poll"):
            return 200, JSON, self._pair_poll(body or {})

        if route == ("GET", "/api/servers"):
            return 200, JSON, {"servers": _masked_servers(self.config.masked()), "active": self.config.get().get("active_server")}

        if route == ("POST", "/api/servers"):
            data = body or {}
            server = self.config.add_server(
                str(data.get("url") or ""),
                data.get("auth"),
                str(data.get("name") or ""),
                make_active=bool(data.get("activate", True)),
            )
            self._cache_clear()
            return 200, JSON, {"server": _mask_one(server)}

        match = re.fullmatch(r"/api/servers/([A-Za-z0-9_-]+)/activate", path)
        if method == "POST" and match:
            server = self.config.activate_server(match.group(1))
            self._cache_clear()
            return 200, JSON, {"server": _mask_one(server), "status": self.server_status(fresh=True)}

        match = re.fullmatch(r"/api/servers/([A-Za-z0-9_-]+)", path)
        if method == "DELETE" and match:
            self.config.remove_server(match.group(1))
            self._cache_clear()
            return 200, JSON, {"ok": True}

        if route == ("GET", "/api/platforms"):
            return 200, JSON, {"platforms": self.platforms()}

        if route == ("GET", "/api/roms"):
            platform = _q(query, "platform") or _q(query, "platform_id")
            search = _q(query, "search") or _q(query, "search_term") or ""
            limit = _int(_q(query, "limit"), 60, 1, 500)
            offset = _int(_q(query, "offset"), 0, 0, 10_000_000)
            sort = _q(query, "sort") or "name"
            return 200, JSON, self.roms(platform, search, limit, offset, sort)

        match = re.fullmatch(r"/api/roms/(\d+)", path)
        if method == "GET" and match:
            return 200, JSON, {"rom": self.rom_detail(int(match.group(1)))}

        match = re.fullmatch(r"/api/cover/(\d+)", path)
        if method == "GET" and match:
            size = _q(query, "size") or "large"
            return self._cover(int(match.group(1)), size)

        match = re.fullmatch(r"/api/platform-logo/(\d+)", path)
        if method == "GET" and match:
            return self._platform_logo(int(match.group(1)))

        if route == ("GET", "/api/emudeck"):
            return 200, JSON, self.emudeck_snapshot()

        if route == ("POST", "/api/emudeck/mapping"):
            data = body or {}
            platform_key = str(data.get("platform") or "")
            if not platform_key:
                raise HttpError(400, "A platform is required.")
            self.config.set_override(
                platform_key,
                {
                    "dir": data.get("dir"),
                    "launcher": data.get("launcher"),
                    "core": data.get("core"),
                },
            )
            invalidate_emudeck()
            self._cache_clear()
            return 200, JSON, self.emudeck_snapshot()

        if route == ("GET", "/api/installs"):
            return 200, JSON, {"installs": self.state.installs(), "history": self.state.history(20)}

        if route == ("POST", "/api/installs/repair"):
            removed = self.state.repair()
            self._cache_clear()
            return 200, JSON, {"removed": removed, "installs": self.state.installs()}

        if route == ("GET", "/api/downloads"):
            return 200, JSON, {"downloads": self.downloads.list()}

        match = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/cancel", path)
        if method == "POST" and match:
            job = self.downloads.cancel(match.group(1))
            if job is None:
                raise HttpError(404, "No such download.")
            return 200, JSON, {"download": job}

        if route == ("POST", "/api/install"):
            data = body or {}
            rom_id = int(data.get("rom_id") or 0)
            if rom_id <= 0:
                raise HttpError(400, "A rom_id is required.")
            rom = self.rom_detail(rom_id)
            mapping = rom.get("mapping") or {}
            if not mapping.get("path"):
                raise HttpError(
                    409,
                    f"EmuDeck has no folder for {rom.get('platform_name') or 'this platform'}. "
                    "Set one in Settings, Emulators.",
                )
            job = self.downloads.enqueue_rom(rom, mapping, self.client())
            self._cache_clear()
            return 200, JSON, {"download": job}

        if route == ("POST", "/api/bios"):
            data = body or {}
            platform_id = int(data.get("platform_id") or 0)
            if platform_id <= 0:
                raise HttpError(400, "A platform_id is required.")
            platform = self.resolve_platform(platform_id)
            firmware = self.firmware(platform_id)
            cfg = self.config.get()
            emu = detect_emudeck(cfg.get("emudeck", {}))
            jobs = self.downloads.enqueue_bios(platform, firmware, emu["bios_root"], self.client())
            return 200, JSON, {
                "downloads": jobs,
                "platform": platform.get("fs_slug"),
                "total": len(firmware),
                "skipped": len(firmware) - len(jobs),
            }

        match = re.fullmatch(r"/api/firmware/(\d+)", path)
        if method == "GET" and match:
            return 200, JSON, {"firmware": self.firmware(int(match.group(1)))}

        if route == ("POST", "/api/play"):
            data = body or {}
            rom_id = int(data.get("rom_id") or 0)
            if rom_id <= 0:
                raise HttpError(400, "A rom_id is required.")
            return 200, JSON, self._play(rom_id, bool(data.get("dry_run")))

        if route == ("GET", "/api/history"):
            return 200, JSON, {"history": self.state.history(30)}

        if route == ("GET", "/api/deeplink"):
            return 200, JSON, {"target": self.deeplinks.take()}

        if route == ("POST", "/api/deeplink"):
            data = body or {}
            raw = data.get("url") if isinstance(data, dict) else None
            if raw:
                target = parse_deeplink(str(raw))
            elif isinstance(data, dict) and data.get("rom"):
                target = {
                    "rom": int(data["rom"]),
                    "platform": str(data.get("platform") or ""),
                    "name": str(data.get("name") or ""),
                    "play": bool(data.get("play")),
                    "server": str(data.get("server") or ""),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            else:
                raise HttpError(400, "A RomMArcade URL is required.")
            if target.get("server"):
                try:
                    self.config.activate_server(str(target["server"]))
                    self._cache_clear()
                except ConfigError:
                    pass
            return 200, JSON, {"target": self.deeplinks.push(target)}

        if route == ("GET", "/api/log"):
            lines = _int(_q(query, "lines"), 200, 10, 2000)
            return 200, JSON, {"lines": _tail(self.log_file, lines)}

        if route == ("GET", "/api/debug"):
            cfg = self.config.masked()
            return 200, JSON, {
                "product": PRODUCT,
                "version": __version__,
                "user_agent": USER_AGENT,
                "config": cfg,
                "python": os.sys.version,
                "pid": os.getpid(),
                "data_dir": data_dir(),
                "config_dir": config_dir(),
            }

        raise HttpError(404, "No such RomMArcade route.")

    # -- actions ----------------------------------------------------------
    def _apply_config(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config.get()
        if "server" in body and body.get("server"):
            incoming = dict(body["server"] or {})
            active = self.config.active_server() or {}
            auth = incoming.get("auth")
            if auth is None:
                auth = active.get("auth") or {"mode": "none"}
            else:
                auth = _unmask_auth(auth, active.get("auth") or {})
            if incoming.get("url"):
                server = self.config.add_server(
                    str(incoming["url"]),
                    auth,
                    str(incoming.get("name") or ""),
                    make_active=True,
                    server_id=active.get("id"),
                )
            elif auth is not None and active:
                server = self.config.update_server(active["id"], {"auth": auth})
            else:
                server = active
            self._cache_clear()
        for key in ("emudeck", "ui", "launch", "downloads", "web"):
            if key in body and isinstance(body[key], dict):
                self.config.update({key: body[key]})
        if body.get("active_server"):
            self.config.activate_server(str(body["active_server"]))
            self._cache_clear()
        if "emudeck" in body:
            invalidate_emudeck()
            self._cache_clear()
        return {"config": self.config.masked(), "status": self.server_status(fresh=True)}

    def _test_server(self, body: dict[str, Any]) -> dict[str, Any]:
        url = str(body.get("url") or "").strip()
        auth = body.get("auth")
        active = self.config.active_server() or {}
        if url and (not auth):
            if active and _same_url(active.get("url"), url):
                auth = active.get("auth")
        if not url and active:
            url = active.get("url", "")
            auth = auth or active.get("auth")
        if not url:
            raise HttpError(400, "A server URL is required.")
        client = RomMClient(url, auth or {"mode": "none"})
        probe = client.probe()
        return {"ok": True, "server": probe}

    def _pair_start(self, body: dict[str, Any]) -> dict[str, Any]:
        url = str(body.get("url") or "").strip()
        if not url:
            raise HttpError(400, "A server URL is required.")
        client = RomMClient(url, {"mode": "none"})
        payload = {
            "client_device_identifier": self._device_identifier,
            "name": str(body.get("name") or socket.gethostname() or "RomMArcade"),
            "client": CLIENT_ID,
            "platform": "linux",
            "client_version": __version__,
            "requested_scopes": REQUESTED_SCOPES,
        }
        data = client.json("POST", "/api/auth/device/init", body=payload)
        if not isinstance(data, dict) or not data.get("device_code"):
            raise HttpError(502, "The server did not start a device pairing.")
        self._pairings[data["device_code"]] = {"url": url, "created": _now()}
        base = client.base_url
        verification = str(data.get("verification_path") or "/pair/device")
        complete = str(data.get("verification_path_complete") or "")
        return {
            "device_code": data["device_code"],
            "user_code": data.get("user_code"),
            "verification_path": verification,
            "verification_url": base + (complete or verification),
            "expires_in": data.get("expires_in"),
            "interval": data.get("interval") or 5,
            "server": base,
        }

    def _pair_poll(self, body: dict[str, Any]) -> dict[str, Any]:
        device_code = str(body.get("device_code") or "").strip()
        if not device_code:
            raise HttpError(400, "A device_code is required.")
        pending = self._pairings.get(device_code)
        url = str(body.get("url") or (pending or {}).get("url") or "")
        if not url:
            raise HttpError(400, "The pairing session expired. Start again.")
        client = RomMClient(url, {"mode": "none"})
        try:
            data = client.json("POST", "/api/auth/device/token", body={"device_code": device_code})
        except RomMError as exc:
            detail = ""
            if isinstance(exc.detail, dict):
                detail = str(exc.detail.get("detail") or "")
            elif isinstance(exc.detail, str):
                detail = exc.detail
            if not detail and exc.status == 400:
                detail = str(exc)
            if detail in ("authorization_pending", "slow_down"):
                return {"status": "pending", "detail": detail}
            if detail == "access_denied":
                return {"status": "denied"}
            if detail == "expired_token":
                self._pairings.pop(device_code, None)
                return {"status": "expired"}
            raise
        if not isinstance(data, dict) or not data.get("access_token"):
            return {"status": "pending"}
        token = str(data["access_token"])
        auth_client = RomMClient(url, {"mode": "token", "token": token})
        me = auth_client.me()
        name = str(me.get("username") or "") or socket.gethostname()
        server = self.config.add_server(url, {"mode": "token", "token": token}, name, make_active=True)
        self._pairings.pop(device_code, None)
        self._cache_clear()
        return {
            "status": "approved",
            "server": _mask_one(server),
            "user": {"id": me.get("id"), "username": me.get("username"), "role": me.get("role")},
            "scopes": data.get("scopes") or [],
        }

    def _play(self, rom_id: int, dry_run: bool) -> dict[str, Any]:
        rom = self.rom_detail(rom_id)
        mapping = rom.get("mapping") or {}
        installed = rom.get("installed")
        if not installed:
            raise HttpError(
                409,
                f"{rom.get('name') or 'This game'} is not installed yet.",
                needs_install=True,
                rom_id=rom_id,
            )
        command = build_command(mapping, installed["path"])
        if dry_run:
            return {"dry_run": True, "command": command, "rom": rom_id, "path": installed["path"]}
        if not os.path.exists(command[0]):
            raise HttpError(409, f"The {mapping.get('launcher_name') or 'emulator'} launcher is missing.")
        log = os.path.join(data_dir(), "launcher.log")
        pid = spawn(command, log)
        self.state.mark_played(rom, rom.get("platform_fs_slug") or mapping.get("platform") or "")
        self.log(f"play rom {rom_id} with {command[0]} pid={pid}")
        return {
            "launched": True,
            "pid": pid,
            "command": command,
            "path": installed["path"],
            "launcher": mapping.get("launcher"),
            "core": mapping.get("core"),
            "log": log,
        }

    # -- covers -----------------------------------------------------------
    def _cover(self, rom_id: int, size: str) -> tuple[int, dict[str, str], bytes]:
        size = "small" if str(size).lower().startswith("s") else "large"
        server = self.active_server()
        if not server:
            raise HttpError(404, "No server configured.")
        key = f"{server.get('id')}-{rom_id}-{size}"
        directory = os.path.join(cache_dir(), "covers", str(server.get("id")))
        path = os.path.join(directory, key + ".img")
        meta_path = path + ".json"
        if os.path.exists(path) and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
                with open(path, "rb") as handle:
                    return 200, {"Content-Type": meta.get("type") or "image/png", "Cache-Control": "max-age=86400"}, handle.read()
            except (OSError, json.JSONDecodeError):
                pass
        rom = self.rom_detail(rom_id)
        relative = rom.get("cover") if size == "large" else (rom.get("cover_small") or rom.get("cover"))
        if not relative:
            raise HttpError(404, "This game has no cover art.")
        if relative.startswith("http://") or relative.startswith("https://"):
            relative = urllib.parse.urlsplit(relative).path
        response = self.client(server).open(relative, timeout=30)
        try:
            declared = response.headers.get("Content-Type") or "image/png"
            data = response.read(COVER_LIMIT + 1)
        finally:
            response.close()
        if len(data) > COVER_LIMIT:
            raise HttpError(502, "That cover is larger than RomMArcade will cache.")
        content_type = _image_type(data, declared)
        os.makedirs(directory, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump({"type": content_type}, handle)
        return 200, {"Content-Type": content_type, "Cache-Control": "max-age=86400"}, data

    def _platform_logo(self, platform_id: int) -> tuple[int, dict[str, str], bytes]:
        server = self.active_server()
        if not server:
            raise HttpError(404, "No server configured.")
        platform = self.resolve_platform(platform_id)
        relative = platform.get("url_logo") or ""
        if not relative:
            raise HttpError(404, "This platform has no logo.")
        directory = os.path.join(cache_dir(), "logos", str(server.get("id")))
        path = os.path.join(directory, f"{platform_id}.img")
        meta_path = path + ".json"
        if os.path.exists(path) and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as handle:
                    meta = json.load(handle)
                with open(path, "rb") as handle:
                    return 200, {"Content-Type": meta.get("type") or "image/png", "Cache-Control": "max-age=604800"}, handle.read()
            except (OSError, json.JSONDecodeError):
                pass
        if relative.startswith("http://") or relative.startswith("https://"):
            # Platform artwork points at the metadata provider (IGDB), not at
            # the RomM host; fetch the absolute URL instead of its path.
            request = urllib.request.Request(relative, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    declared = response.headers.get("Content-Type") or "image/jpeg"
                    data = response.read(2 * 1024 * 1024)
            except (urllib.error.URLError, TimeoutError) as exc:
                raise HttpError(502, f"That platform logo could not be fetched: {exc}") from None
        else:
            response = self.client(server).open(relative, timeout=20)
            try:
                declared = response.headers.get("Content-Type") or "image/png"
                data = response.read(2 * 1024 * 1024)
            finally:
                response.close()
        content_type = _image_type(data, declared)
        os.makedirs(directory, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump({"type": content_type}, handle)
        return 200, {"Content-Type": content_type, "Cache-Control": "max-age=604800"}, data

    # -- static -----------------------------------------------------------
    def _static(self, method: str, path: str) -> tuple[int, dict[str, str], Any]:
        if method not in ("GET", "HEAD"):
            raise HttpError(405, "Only GET is supported for files.")
        clean = urllib.parse.unquote(path)
        clean = posixpath.normpath(clean).lstrip("/")
        if clean in ("", "."):
            clean = "index.html"
        candidate = os.path.join(WEB_ROOT, *clean.split("/"))
        if not os.path.abspath(candidate).startswith(os.path.abspath(WEB_ROOT)):
            raise HttpError(404, "Not found.")
        if not os.path.isfile(candidate):
            if "." in os.path.basename(clean):
                raise HttpError(404, "Not found.")
            candidate = os.path.join(WEB_ROOT, "index.html")
        with open(candidate, "rb") as handle:
            data = handle.read()
        content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        # A local app updates by copying files; always revalidate so a fix is
        # visible on the next reload instead of an hour later.
        cache = "no-cache"
        return 200, {"Content-Type": content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type.endswith("javascript") else ""), "Cache-Control": cache}, data


JSON = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}


class Handler(BaseHTTPRequestHandler):
    server_version = "RomMArcade/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # route access logs to our file
        try:
            self.server.app.log("%s %s" % (self.address_string(), fmt % args))  # type: ignore[attr-defined]
        except Exception:
            pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        body = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            self._send(413, {"Content-Type": "application/json"}, {"error": "Request body too large."}, head=method == "HEAD")
            return
        if length:
            raw = self.rfile.read(length)
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send(400, {"Content-Type": "application/json"}, {"error": "The request body is not JSON."}, head=method == "HEAD")
                    return
        try:
            status, headers, payload = self.server.app.dispatch(method, parsed.path, query, body)  # type: ignore[attr-defined]
        except Exception as exc:  # dispatch already catches, this is a last resort
            status, headers, payload = 500, {"Content-Type": "application/json"}, {"error": str(exc)}
        self._send(status, headers, payload, head=method == "HEAD")

    def _send(self, status: int, headers: dict[str, str], payload: Any, head: bool = False) -> None:
        if isinstance(payload, (dict, list)):
            data = _json_bytes(payload)
            headers = {**headers, "Content-Type": headers.get("Content-Type", "application/json; charset=utf-8")}
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = str(payload).encode("utf-8")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app: App):
        super().__init__(address, handler)
        self.app = app


def run(host: str = "0.0.0.0", port: int = 8790, config: ConfigStore | None = None) -> None:
    app = App(config)
    httpd = Server((host, int(port)), Handler, app)
    app.log(f"RomMArcade {__version__} listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    finally:
        app.downloads.shutdown()
        httpd.server_close()
        app.log("RomMArcade stopped")


# -- small utilities -----------------------------------------------------
def _q(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return values[0].strip() if values else ""


def _int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _sort_params(sort: str) -> tuple[str | None, str]:
    table = {
        "name": ("name", "asc"),
        "name_desc": ("name", "desc"),
        "newest": ("added", "desc"),
        "oldest": ("added", "asc"),
        "size": ("size", "desc"),
        "size_asc": ("size", "asc"),
    }
    return table.get((sort or "").lower(), ("name", "asc"))


def _same_url(left: str, right: str) -> bool:
    from .config import normalize_url

    try:
        return normalize_url(left) == normalize_url(right)
    except ConfigError:
        return False


def _unmask_auth(auth: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    out = dict(auth)
    for key in ("token", "password"):
        value = str(out.get(key) or "")
        if value and "•" in value:
            out[key] = existing.get(key) or ""
    return out


def _masked_servers(masked: dict[str, Any]) -> list[dict[str, Any]]:
    return [_mask_one(server) for server in masked.get("servers", [])]


def _mask_one(server: dict[str, Any]) -> dict[str, Any]:
    out = dict(server)
    auth = dict(out.get("auth") or {})
    for key in ("token", "password"):
        if key in auth and auth[key]:
            auth[key] = "•" * 8
            auth["has_secret"] = True
    out["auth"] = auth
    return out


def _tail(path: str, lines: int) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            data = handle.read().decode("utf-8", "replace")
        return data.splitlines()[-lines:]
    except OSError:
        return []


def _release_year(value: Any) -> int | None:
    """RomM stores first_release_date as epoch milliseconds; the chip wants a year."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).year
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        match = re.match(r"\s*(\d{4})", value)
        if match:
            return int(match.group(1))
    return None


def _image_type(data: bytes, declared: str = "") -> str:
    """Trust the bytes over the header: RomM serves JPEGs from .png covers."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip()[:5] in (b"<?xml", b"<svg "):
        return "image/svg+xml"
    return declared if declared.startswith("image/") else "application/octet-stream"
