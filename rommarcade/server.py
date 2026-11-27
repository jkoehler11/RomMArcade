"""The local RomMArcade service: JSON API, static UI, downloads and launching."""

from __future__ import annotations

import glob
import io
import json
import mimetypes
import os
import posixpath
import re
import shutil
import socket
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import base64
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:  # Optional: turns IGDB's black/white-backed logos into tidy tiles.
    from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageOps, ImageStat

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - the app still runs, just plainer
    PIL_AVAILABLE = False

from . import CLIENT_ID, PRODUCT, PRODUCT_TAGLINE, REQUESTED_SCOPES, USER_AGENT, __version__
from .config import ConfigError, ConfigStore, cache_dir, config_dir, data_dir, log_path
from .deeplink import DeepLinkError, DeepLinkQueue, parse_deeplink
from .downloads import DownloadManager
from .emudeck import (
    EmuDeckError,
    bios_target,
    build_command,
    cemu_keys_status,
    download_emulator_storage,
    migration_move,
    migration_snapshot,
    repair_emulator_bios,
    create_library,
    detect as detect_emudeck,
    install_path,
    invalidate_cache as invalidate_emudeck,
    library_roots,
    plan_library,
    platform_choices,
    platform_mapping,
    remove_game_saves,
    rom_file,
    rom_target_name,
    spawn,
    spawn_process,
    suggest_library_root,
    sync_cemu_keys,
)
from .romm import RomMClient, RomMError
from .saves import SaveSyncError, SaveSyncService
from .state import StateStore, ensure_dirs

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MAX_BODY = 2 * 1024 * 1024
COVER_LIMIT = 12 * 1024 * 1024
MAX_LIBRARY = 5000
TOOL_PLATFORMS = {"emulators", "cloud", "remoteplay"}
SAVE_FILE_EXTENSIONS = {
    ".srm", ".sav", ".state", ".st", ".ss", ".rtc", ".eep", ".fla", ".mpk", ".mcd",
    ".mem", ".nv", ".ps2", ".psu", ".mcr", ".sra", ".frz", ".bcr", ".bcs", ".gci",
}
TOOL_EXTENSIONS = {
    ".sh", ".exe", ".appimage", ".desktop", ".bat", ".cmd", ".py", ".dll", ".so",
    ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".txt", ".md", ".json", ".xml", ".ini",
    ".cfg", ".log", ".toml", ".yml", ".yaml", ".lock", ".db", ".sqlite", ".url", ".webloc",
    ".xpso", ".xsh", ".header", ".gpd", ".d3d12", ".rtv",
}


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
        self._bios_done_seen: set[str] = set()
        self.started = _now()
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._pairings: dict[str, dict[str, Any]] = {}
        self._device_identifier = self._load_device_identifier()
        self._reachability: tuple[float, dict[str, Any]] | None = None
        self.save_sync = SaveSyncService(self.config, self.log, self._device_identifier)

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

    def emudeck_enabled(self) -> bool:
        return bool(self.config.get().get("emudeck", {}).get("enabled", True))

    def require_emudeck(self) -> None:
        if not self.emudeck_enabled():
            raise HttpError(
                409,
                "Installs and launches are turned off. Turn the EmuDeck integration back on in Settings, EmuDeck.",
            )

    # -- RomM data --------------------------------------------------------
    def library_roms(self) -> list[dict[str, Any]]:
        """Every visible RomM entry that is actually a game.

        RomM happily scans whatever sits in a platform folder, so an emulator
        install (xenia.sh, cache/, LICENSE) can appear as a "rom".  The library
        only takes entries with a real game file; emulator management belongs
        to EmuDeck, not to the game shelf.
        """
        cached = self._cache_get("library", 45)
        if cached is not None:
            return cached
        items: list[dict[str, Any]] = []
        try:
            offset = 0
            while offset < MAX_LIBRARY:
                data = self.client().roms(limit=500, offset=offset, with_files=True)
                batch = data.get("items") or []
                if not batch:
                    break
                items.extend(_trim_rom(rom) for rom in batch if _is_game_rom(rom))
                offset += len(batch)
                total = int(data.get("total") or 0)
                if total and offset >= total:
                    break
        except RomMError:
            return []
        return self._cache_set("library", collapse_sibling_roms(items))

    def _game_ids(self) -> set[int]:
        return {int(rom["id"]) for rom in self.library_roms() if rom.get("id") is not None}

    def game_installs(self) -> list[dict[str, Any]]:
        ids = self._game_ids()
        return [entry for entry in self.state.installs() if int(entry.get("rom_id") or 0) in ids]

    def game_history(self, limit: int = 30) -> list[dict[str, Any]]:
        ids = self._game_ids()
        history = [entry for entry in self.state.history(100) if int(entry.get("rom_id") or 0) in ids]
        return history[:limit]

    def uninstall(self, rom_id: int, remove_saves: bool = True) -> dict[str, Any]:
        """Remove an installed ROM, and optionally its EmuDeck saves."""
        entry = self.state.install_for(int(rom_id))
        if not entry:
            raise HttpError(404, "That game is not installed on this machine.")
        for job in self.downloads.list():
            if int(job.get("rom_id") or 0) == int(rom_id) and job.get("status") in (
                "queued",
                "downloading",
                "extracting",
            ):
                raise HttpError(409, "That game is still installing.")
        target = os.path.abspath(entry.get("path") or "")
        mapping: dict[str, Any] = {}
        try:
            platform = self.resolve_platform(entry.get("platform") or entry.get("platform_slug"))
            mapping = platform.get("local") or {}
        except HttpError:
            mapping = {}
        emu = detect_emudeck(self.config.get())
        allowed = [mapping.get("path") or "", emu.get("roms_root") or ""]
        safe = False
        for base in allowed:
            if not base:
                continue
            try:
                if os.path.commonpath([os.path.abspath(base), target]) == os.path.abspath(base):
                    safe = True
                    break
            except ValueError:
                continue
        if not target or not safe:
            raise HttpError(400, "Refusing to remove a path outside this machine's game folders.")
        target_is_file = os.path.isfile(target)
        stem = os.path.basename(target.rstrip(os.sep))
        if target_is_file:
            stem = os.path.splitext(stem)[0]
        if os.path.islink(target) or target_is_file:
            os.unlink(target)
        elif os.path.isdir(target):
            shutil.rmtree(target)
        elif os.path.exists(target):
            raise HttpError(409, "That install is not a file or folder RomMArcade can remove.")
        removed_saves: list[str] = []
        if remove_saves and emu.get("detected"):
            platform_dir = str(mapping.get("dir") or entry.get("platform_slug") or entry.get("platform") or "")
            removed_saves = remove_game_saves(
                emu.get("root") or "", platform_dir, stem,
                str(mapping.get("launcher") or ""),
            )
        self.state.forget(int(rom_id))
        self._cache_clear()
        self.log(
            f"uninstalled rom {rom_id} -> {entry.get('path')} "
            f"(saves removed: {len(removed_saves)})"
        )
        return {
            "removed": True,
            "rom_id": int(rom_id),
            "path": entry.get("path"),
            "saves_removed": len(removed_saves),
            "saves": removed_saves,
        }

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
        enabled = self.emudeck_enabled()
        emu = detect_emudeck(cfg)
        overrides = emu_cfg.get("overrides", {})
        installs = self.state.installs()
        counts: dict[str, int] = {}
        for entry in installs:
            counts[str(entry.get("platform") or "")] = counts.get(str(entry.get("platform") or ""), 0) + 1
        game_counts: dict[str, int] = {}
        for game in self.library_roms():
            if game.get("hidden_sibling"):
                continue
            key = str(game.get("platform_fs_slug") or game.get("platform_slug") or "")
            game_counts[key] = game_counts.get(key, 0) + 1
        out = []
        for platform in raw:
            fs_slug = str(platform.get("fs_slug") or platform.get("slug") or "")
            if enabled:
                mapping = platform_mapping(platform, emu, emu["launchers"], emu["cores"], overrides)
            else:
                mapping = {
                    "platform": fs_slug,
                    "slug": platform.get("slug"),
                    "name": platform.get("display_name") or platform.get("name") or fs_slug,
                    "dir": "",
                    "path": "",
                    "exists": False,
                    "launcher": "",
                    "launcher_name": "",
                    "launcher_path": "",
                    "launcher_ready": False,
                    "core": "",
                    "source": "disabled",
                }
            mapping["enabled"] = enabled
            item = dict(platform)
            item["logo_url"] = f"/api/platform-logo/{platform.get('id')}" if platform.get("url_logo") else ""
            item["logo_normalized"] = PIL_AVAILABLE
            item["game_count"] = game_counts.get(fs_slug, 0)
            item["is_game_platform"] = fs_slug.lower() not in TOOL_PLATFORMS
            item["local"] = {
                **_public_mapping(mapping, enabled),
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
        emu = detect_emudeck(cfg)
        platforms = []
        options: dict[str, Any] = {}
        if self.active_server():
            try:
                for platform in self.platforms():
                    platforms.append(platform["local"])
                    if platform.get("is_game_platform") is False:
                        continue
                    key = str(platform.get("fs_slug") or platform.get("slug") or "")
                    if key:
                        options[key] = platform_choices(key, emu["launchers"], emu["cores"])
            except (RomMError, HttpError):
                pass
        emu = dict(emu)
        emu["enabled"] = self.emudeck_enabled()
        emu["installer"] = _find_emudeck_installer()
        emu["platforms"] = platforms
        emu["options"] = options
        emu["overrides"] = emu_cfg.get("overrides", {})
        return emu

    def library_snapshot(self) -> dict[str, Any]:
        """The plain game-folder setup, with one row per platform worth creating."""
        cfg = self.config.get()
        library = library_roots(cfg)
        rows: list[dict[str, Any]] = []
        if self.active_server():
            try:
                platforms = self.platforms()
            except (RomMError, HttpError):
                platforms = []
            for platform in platforms:
                if platform.get("is_game_platform") is False:
                    continue
                games = int(platform.get("game_count") or platform.get("rom_count") or 0)
                if games <= 0:
                    continue
                slug = str(platform.get("fs_slug") or platform.get("slug") or "")
                if not slug:
                    continue
                local = platform.get("local") or {}
                rows.append(
                    {
                        "platform": slug,
                        "name": platform.get("display_name") or platform.get("name") or slug,
                        "games": games,
                        "folder": local.get("dir") or slug,
                        "dir": local.get("dir") or "",
                        "path": local.get("path") or "",
                        "exists": bool(local.get("exists")),
                    }
                )
        return {
            "mode": library["mode"],
            "configured": bool(library["root"]),
            "root": library["root"],
            "roms_root": library["roms_root"],
            "bios_root": library["bios_root"],
            "exists": bool(library["exists"]),
            "emudeck_detected": bool(detect_emudeck(cfg)["detected"]),
            "suggestions": {
                "romm": suggest_library_root("romm"),
                "custom": suggest_library_root("custom"),
            },
            "platforms": rows,
            "mapped": len([row for row in rows if row["exists"]]),
        }

    def save_library(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create or clear the game folder layout and remember the choice."""
        mode = str(body.get("mode") or "").strip().lower()
        if mode in ("", "none", "later", "skip"):
            self.config.update({"library": {"mode": "", "root": "", "bios_root": ""}})
            invalidate_emudeck()
            self._cache_clear()
            self.log("cleared the game folder setup")
            return {"library": self.library_snapshot(), "created": []}
        plan = plan_library(mode, str(body.get("root") or ""))
        platforms: list[dict[str, Any]] = []
        if self.active_server():
            try:
                platforms = self.platforms()
            except (RomMError, HttpError):
                platforms = []
        created: list[str] = []
        if body.get("create", True):
            created = create_library(plan, platforms)["created"]
        self.config.update(
            {
                "library": {
                    "mode": plan["mode"],
                    "root": plan["root"],
                    "bios_root": plan["bios_root"],
                }
            }
        )
        invalidate_emudeck()
        self._cache_clear()
        self.log(
            f"game library {plan['mode']} -> {plan['roms_root']} "
            f"({len(created)} folders created)"
        )
        return {"library": self.library_snapshot(), "created": created}

    def rom_detail(self, rom_id: int, use_cache: bool = True) -> dict[str, Any]:
        key = f"rom:{self.config.get().get('active_server')}:{int(rom_id)}"
        if use_cache:
            cached = self._cache_get(key, 120)
            if cached is not None:
                # Install state changes without touching the RomM metadata, so
                # refresh it even when the rest of the detail came from cache.
                cached["installed"] = self.state.install_for(int(rom_id))
                return cached
        rom = self.client().rom(int(rom_id))
        if not rom.get("files"):
            data = self.client().roms(rom_ids=[int(rom_id)], with_files=True, limit=1)
            items = data.get("items") or []
            if items:
                rom = items[0]
        if not _is_game_rom(rom):
            raise HttpError(404, "That RomM entry is not a game.")
        # A hidden sibling still opens its own page; copy its version group from
        # the library so the picker is there too.
        library = next(
            (entry for entry in self.library_roms() if int(entry.get("id") or 0) == int(rom_id)),
            None,
        )
        if library:
            for field in ("version_count", "versions", "hidden_sibling", "version_of"):
                if library.get(field) is not None:
                    rom[field] = library[field]
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
            "mapping": _public_mapping(mapping, True),
            "version_count": int(rom.get("version_count") or 1),
            "versions": rom.get("versions") or [],
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
        platform = None
        if platform_key and platform_key not in ("all", "*"):
            platform = self.resolve_platform(platform_key)
        items = [rom for rom in self.library_roms() if not rom.get("hidden_sibling")]
        if platform is not None:
            platform_id = int(platform.get("id") or 0)
            slugs = {str(platform.get("slug") or "").lower(), str(platform.get("fs_slug") or "").lower()}
            items = [
                rom for rom in items
                if int(rom.get("platform_id") or 0) == platform_id
                or str(rom.get("platform_slug") or "").lower() in slugs
                or str(rom.get("platform_fs_slug") or "").lower() in slugs
            ]
        term = (search or "").strip().casefold()
        if term:
            items = [
                rom for rom in items
                if term in str(rom.get("name") or "").casefold()
                or term in str(rom.get("fs_name") or "").casefold()
                or term in str(rom.get("platform_display_name") or "").casefold()
                or term in str(rom.get("platform_fs_slug") or "").casefold()
            ]
        order_by, order_dir = _sort_params(sort)
        if order_by == "name":
            items.sort(key=lambda rom: str(rom.get("name") or "").casefold(), reverse=order_dir == "desc")
        elif order_by == "size":
            items.sort(key=lambda rom: int(rom.get("fs_size_bytes") or 0), reverse=order_dir == "desc")
        else:
            items.sort(
                key=lambda rom: str(rom.get("created_at") or rom.get("updated_at") or rom.get("id") or ""),
                reverse=order_dir == "desc",
            )
        total = len(items)
        page = items[offset : offset + limit]
        return {
            "items": [self.enrich_rom(rom) for rom in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _bios_context(self, platform_id: int) -> dict[str, Any]:
        cfg = self.config.get()
        emu = detect_emudeck(cfg)
        root = emu.get("bios_root") or ""
        platform = self.resolve_platform(platform_id)
        slug = str(platform.get("fs_slug") or platform.get("slug") or "")
        mapping = platform_mapping(
            platform, emu, emu.get("launchers") or [], emu.get("cores") or [],
            (cfg.get("emudeck") or {}).get("overrides") or {},
        )
        folder = os.path.join(root, slug) if root and slug and os.path.isdir(os.path.join(root, slug)) else ""
        storage_root = os.path.join(emu.get("root") or "", "storage") if emu.get("root") else ""
        return {
            "root": root, "storage_root": storage_root, "platform": platform,
            "slug": slug, "mapping": mapping, "folder": folder,
        }

    def bios_local(self, platform_id: int) -> dict[str, Any]:
        context = self._bios_context(platform_id)
        root = context["root"]
        slug = context["slug"]
        mapping = context["mapping"]
        folder = context["folder"]
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        if folder:
            for name in sorted(os.listdir(folder)):
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    files.append({"name": name, "path": path, "size": os.path.getsize(path)})
                    seen.add(path)
        for name in mapping.get("bios_required") or []:
            path = os.path.join(root, name) if root else ""
            if path and os.path.isfile(path) and path not in seen:
                files.append({"name": name, "path": path, "size": os.path.getsize(path)})
        return {
            "platform": slug,
            "folder": folder or root,
            "files": files,
            "bios_status": [dict(entry) for entry in (mapping.get("bios_status") or [])],
            "bios_required": list(mapping.get("bios_required") or []),
            "bios_missing": list(mapping.get("bios_missing") or []),
            "bios_folder": root,
        }

    def delete_bios_local(self, body: dict[str, Any]) -> dict[str, Any]:
        platform_id = int(body.get("platform_id") or 0)
        name = str(body.get("name") or "").strip()
        if platform_id <= 0 or not name:
            raise ConfigError("Choose a BIOS file to delete.")
        if os.path.basename(name) != name or name in (".", ".."):
            raise ConfigError("That is not a BIOS file name.")
        context = self._bios_context(platform_id)
        root = context["root"]
        if not root:
            raise EmuDeckError("No EmuDeck BIOS folder is set.")
        mapping = context["mapping"]
        folder = context["folder"] or root
        if folder == root and name not in (mapping.get("bios_required") or []):
            raise ConfigError(
                "Only this emulator's required BIOS files can be deleted from the shared BIOS folder."
            )
        real_root = os.path.realpath(root)
        path = os.path.realpath(os.path.join(folder, name))
        try:
            inside = os.path.commonpath([real_root, path]) == real_root
        except ValueError:
            inside = False
        if not inside:
            raise ConfigError("That file is outside the BIOS folder.")
        if not os.path.isfile(path):
            raise HttpError(404, f"{name} is not in the BIOS folder.")
        os.unlink(path)
        self._cache_clear()
        return self.bios_local(platform_id)

    def downloads_payload(self) -> dict[str, Any]:
        jobs = self.downloads.list()
        for job in jobs:
            if (job.get("kind") == "bios" and job.get("status") == "done"
                    and job.get("id") not in self._bios_done_seen):
                self._bios_done_seen.add(str(job.get("id")))
                self._cache_clear()
                break
        return {"downloads": jobs}

    def _firmware_action(self, action: str, call):
        try:
            return call()
        except RomMError as exc:
            if exc.status == 403:
                raise ConfigError(
                    f"RomM refused the {action}: this client token needs the firmware.write scope. "
                    "Re-pair the server or create a token with firmware.write in RomM."
                ) from None
            raise

    def upload_firmware(self, body: dict[str, Any]) -> dict[str, Any]:
        platform_id = int(body.get("platform_id") or 0)
        file_name = str(body.get("file_name") or "").strip()
        encoded = body.get("content") or ""
        if platform_id <= 0 or not file_name or not encoded:
            raise ConfigError("Choose a BIOS file and a platform to upload it to.")
        try:
            content = base64.b64decode(str(encoded), validate=True)
        except (ValueError, TypeError):
            raise ConfigError("The BIOS file could not be decoded.")
        if not content:
            raise ConfigError("The BIOS file is empty.")
        if len(content) > 64 * 1024 * 1024:
            raise ConfigError("That BIOS file is larger than 64 MB.")
        result = self._firmware_action(
            "upload", lambda: self.client().upload_firmware(platform_id, file_name, content)
        )
        return {"uploaded": result, "firmware": self.firmware(platform_id)}

    def delete_firmware(self, firmware_id: int) -> dict[str, Any]:
        self._firmware_action("delete", lambda: self.client().delete_firmware(int(firmware_id)))
        return {"deleted": int(firmware_id)}

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
        emu = detect_emudeck(cfg)
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
            emu = detect_emudeck(cfg)
            return 200, JSON, {
                "product": PRODUCT,
                "version": __version__,
                "tagline": PRODUCT_TAGLINE,
                "configured": bool(self.active_server()),
                "server": self.server_status(),
                "emudeck": {
                    "enabled": self.emudeck_enabled(),
                    "mode": emu["mode"],
                    "detected": bool(emu["detected"]),
                    "usable": bool(emu["usable"]),
                    "root": emu["root"],
                    "installer": _find_emudeck_installer(),
                    "launchers": len([l for l in emu["launchers"] if l.get("ready")]),
                    "platforms": len(emu["platforms"]) if "platforms" in emu else 0,
                    "library": {
                        key: emu["library"].get(key)
                        for key in ("mode", "root", "roms_root", "bios_root", "configured", "exists")
                    },
                },
                "web_port": int(cfg.get("web", {}).get("port") or 8790),
                "device_identifier": self._device_identifier,
                "save_sync": self.save_sync.snapshot(),
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

        match = re.fullmatch(r"/api/roms/(\d+)/saves", path)
        if method == "GET" and match:
            return 200, JSON, self.rom_saves(int(match.group(1)))

        match = re.fullmatch(r"/api/roms/(\d+)/saves/restore", path)
        if method == "POST" and match:
            data = body or {}
            save_id = int(data.get("save_id") or 0)
            if save_id <= 0:
                raise HttpError(400, "A save_id is required.")
            return 200, JSON, self.restore_rom_save(
                int(match.group(1)), save_id, bool(data.get("play"))
            )

        match = re.fullmatch(r"/api/roms/(\d+)/saves/upload", path)
        if method == "POST" and match:
            data = body or {}
            return 200, JSON, self.upload_rom_save(int(match.group(1)), str(data.get("name") or ""))

        match = re.fullmatch(r"/api/roms/(\d+)/saves/slot", path)
        if method == "POST" and match:
            data = body or {}
            return 200, JSON, self.set_rom_save_slot(int(match.group(1)), str(data.get("slot") or ""))

        match = re.fullmatch(r"/api/roms/(\d+)/saves/slots", path)
        if method == "POST" and match:
            data = body or {}
            return 200, JSON, self.create_rom_save_slot(
                int(match.group(1)), str(data.get("slot") or ""), bool(data.get("seed", True))
            )

        match = re.fullmatch(r"/api/saves/(\d+)/download", path)
        if method == "GET" and match:
            return self.save_download(int(match.group(1)))

        match = re.fullmatch(r"/api/saves/(\d+)/(favorite|rename|visibility|delete|labels)", path)
        if method == "POST" and match:
            return 200, JSON, self.update_save(int(match.group(1)), match.group(2), body or {})

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

        if route == ("POST", "/api/emudeck/rescan"):
            self.require_emudeck()
            invalidate_emudeck()
            self._cache_clear()
            self.log("rescanned EmuDeck launchers and platforms")
            return 200, JSON, self.emudeck_snapshot()

        if route == ("GET", "/api/emudeck/migrate/scan"):
            return 200, JSON, migration_snapshot(self.config.get())

        if route == ("POST", "/api/emudeck/migrate/move"):
            result = migration_move(self.config.get())
            self._cache_clear()
            invalidate_emudeck()
            return 200, JSON, result

        if route == ("POST", "/api/emudeck/open-installer"):
            self.require_emudeck()
            installer = _find_emudeck_installer()
            if not installer:
                raise HttpError(404, "EmuDeck's installer was not found on this machine.")
            if not os.access(installer, os.X_OK):
                raise HttpError(409, f"EmuDeck's installer is not executable: {installer}")
            log = os.path.join(data_dir(), "launcher.log")
            pid = spawn([installer], log)
            self.log(f"opened EmuDeck installer {installer} pid={pid}")
            return 200, JSON, {"launched": True, "pid": pid, "path": installer}

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

        if route == ("GET", "/api/library"):
            return 200, JSON, self.library_snapshot()

        if route == ("POST", "/api/library"):
            return 200, JSON, self.save_library(body if isinstance(body, dict) else {})

        if route == ("GET", "/api/installs"):
            return 200, JSON, {"installs": self.game_installs(), "history": self.game_history(20)}

        if route == ("POST", "/api/installs/repair"):
            removed = self.state.repair()
            self._cache_clear()
            return 200, JSON, {"removed": removed, "installs": self.game_installs()}

        match = re.fullmatch(r"/api/installs/(\d+)/uninstall", path)
        if method == "POST" and match:
            data = body if isinstance(body, dict) else {}
            return 200, JSON, self.uninstall(
                int(match.group(1)),
                bool(data.get("remove_saves", True)),
            )

        if route == ("GET", "/api/downloads"):
            return 200, JSON, self.downloads_payload()

        match = re.fullmatch(r"/api/downloads/([A-Za-z0-9_-]+)/cancel", path)
        if method == "POST" and match:
            job = self.downloads.cancel(match.group(1))
            if job is None:
                raise HttpError(404, "No such download.")
            return 200, JSON, {"download": job}

        if route == ("POST", "/api/install"):
            self.require_emudeck()
            data = body or {}
            rom_id = int(data.get("rom_id") or 0)
            if rom_id <= 0:
                raise HttpError(400, "A rom_id is required.")
            rom = self.rom_detail(rom_id)
            mapping = rom.get("mapping") or {}
            if not mapping.get("path"):
                raise HttpError(
                    409,
                    f"No game folder is set for {rom.get('platform_name') or 'this platform'}. "
                    "Choose where games should be stored in Settings, Emulators.",
                )
            job = self.downloads.enqueue_rom(rom, mapping, self.client())
            return 200, JSON, {"download": job}

        if route == ("POST", "/api/bios"):
            self.require_emudeck()
            data = body or {}
            platform_id = int(data.get("platform_id") or 0)
            if platform_id <= 0:
                raise HttpError(400, "A platform_id is required.")
            platform = self.resolve_platform(platform_id)
            firmware = self.firmware(platform_id)
            firmware_id = int(data.get("firmware_id") or 0)
            if firmware_id:
                firmware = [item for item in firmware if int(item.get("id") or 0) == firmware_id]
                if not firmware:
                    raise HttpError(404, "No such BIOS file in RomM for this platform.")
            cfg = self.config.get()
            emu = detect_emudeck(cfg)
            jobs = self.downloads.enqueue_bios(platform, firmware, emu["bios_root"], self.client())
            return 200, JSON, {
                "downloads": jobs,
                "platform": platform.get("fs_slug"),
                "total": len(firmware),
                "skipped": len(firmware) - len(jobs),
            }

        match = re.fullmatch(r"/api/bios/local/(\d+)", path)
        if method == "GET" and match:
            return 200, JSON, self.bios_local(int(match.group(1)))

        if route == ("POST", "/api/bios/local/delete"):
            return 200, JSON, self.delete_bios_local(body or {})

        if route == ("POST", "/api/bios/repair"):
            data = body or {}
            platform_id = int(data.get("platform_id") or 0)
            if platform_id <= 0:
                raise HttpError(400, "A platform_id is required.")
            context = self._bios_context(platform_id)
            result = repair_emulator_bios(
                context["mapping"].get("launcher") or "",
                context["root"],
                context["slug"],
            )
            self._cache_clear()
            return 200, JSON, result

        if route == ("POST", "/api/storage/download"):
            data = body or {}
            platform_id = int(data.get("platform_id") or 0)
            if platform_id <= 0:
                raise HttpError(400, "A platform_id is required.")
            kind = str(data.get("kind") or "hdd").strip().lower()
            context = self._bios_context(platform_id)
            try:
                result = download_emulator_storage(
                    context["mapping"].get("launcher") or "",
                    context["storage_root"],
                    kind,
                )
            except EmuDeckError as exc:
                raise HttpError(502, str(exc)) from exc
            self._cache_clear()
            return 200, JSON, result

        if route == ("POST", "/api/firmware/upload"):
            result = self.upload_firmware(body or {})
            self._cache_clear()
            return 200, JSON, result

        match = re.fullmatch(r"/api/firmware/(\d+)/delete", path)
        if method == "POST" and match:
            result = self.delete_firmware(int(match.group(1)))
            self._cache_clear()
            return 200, JSON, result

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
            return 200, JSON, {"history": self.game_history(30)}

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
        for key in ("emudeck", "library", "ui", "launch", "downloads", "web"):
            if key in body and isinstance(body[key], dict):
                self.config.update({key: body[key]})
        if body.get("active_server"):
            self.config.activate_server(str(body["active_server"]))
            self._cache_clear()
        if "emudeck" in body or "library" in body:
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

    # -- saves ------------------------------------------------------------
    def _saves_context(self, rom_id: int) -> tuple[dict[str, Any], dict[str, Any], str]:
        rom = self.rom_detail(int(rom_id))
        mapping = rom.get("mapping") or {}
        emu_root = detect_emudeck(self.config.get()).get("root") or ""
        return rom, mapping, emu_root

    def rom_saves(self, rom_id: int) -> dict[str, Any]:
        rom, mapping, emu_root = self._saves_context(rom_id)
        return self.save_sync.saves_for(rom, mapping, emu_root, self.client(), self.active_server())

    def restore_rom_save(self, rom_id: int, save_id: int, play: bool) -> dict[str, Any]:
        self.require_emudeck()
        rom, mapping, emu_root = self._saves_context(rom_id)
        try:
            result = self.save_sync.restore_save(
                rom, mapping, emu_root, self.client(), self.active_server(), save_id
            )
        except SaveSyncError as exc:
            raise HttpError(409, str(exc), save_sync=exc.summary) from None
        except RomMError as exc:
            raise HttpError(409 if exc.status == 409 else 502, str(exc)) from None
        if play:
            result["play"] = self._play(int(rom_id), False)
        return result

    def upload_rom_save(self, rom_id: int, name: str) -> dict[str, Any]:
        self.require_emudeck()
        rom, mapping, emu_root = self._saves_context(rom_id)
        try:
            return self.save_sync.upload_local(
                rom, mapping, emu_root, self.client(), self.active_server(), name
            )
        except SaveSyncError as exc:
            raise HttpError(409, str(exc), save_sync=exc.summary) from None
        except RomMError as exc:
            raise HttpError(409 if exc.status == 409 else 502, str(exc)) from None

    def set_rom_save_slot(self, rom_id: int, slot: str) -> dict[str, Any]:
        rom, mapping, emu_root = self._saves_context(rom_id)
        try:
            self.save_sync.set_active_slot(self.active_server(), int(rom_id), slot)
        except SaveSyncError as exc:
            raise HttpError(400, str(exc)) from None
        return self.save_sync.saves_for(rom, mapping, emu_root, self.client(), self.active_server())

    def create_rom_save_slot(self, rom_id: int, slot: str, seed: bool = True) -> dict[str, Any]:
        rom, mapping, emu_root = self._saves_context(rom_id)
        try:
            created = self.save_sync.create_slot(
                rom, mapping, emu_root, self.client(), self.active_server(), slot, seed
            )
        except SaveSyncError as exc:
            raise HttpError(400, str(exc)) from None
        except RomMError as exc:
            raise HttpError(409 if exc.status == 409 else 502, str(exc)) from None
        snapshot = self.save_sync.saves_for(rom, mapping, emu_root, self.client(), self.active_server())
        snapshot["created"] = created
        return snapshot

    def save_download(self, save_id: int) -> tuple[int, dict[str, str], bytes]:
        try:
            data, content_type, name = self.save_sync.download_save(self.client(), save_id)
        except SaveSyncError as exc:
            raise HttpError(409, str(exc)) from None
        quoted = urllib.parse.quote(name)
        return 200, {
            "Content-Type": content_type or "application/octet-stream",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        }, data

    def update_save(self, save_id: int, action: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self.client()
        if action == "favorite":
            save = client.update_save_favorite(save_id, bool(body.get("is_favorite")))
        elif action == "visibility":
            save = client.update_save_visibility(save_id, bool(body.get("is_public")))
        elif action == "labels":
            labels = [str(label) for label in (body.get("labels") or []) if str(label).strip()]
            save = client.update_save_labels(save_id, labels)
        elif action == "rename":
            name = str(body.get("file_name") or "").strip()
            if not name:
                raise HttpError(400, "A file name is required.")
            save = client.rename_save(save_id, name)
        else:  # delete
            client.delete_saves([int(save_id)])
            self.log(f"deleted RomM save {save_id}")
            return {"deleted": True, "id": int(save_id)}
        return {"save": save}

    def _play(self, rom_id: int, dry_run: bool) -> dict[str, Any]:
        self.require_emudeck()
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
        emu_root = detect_emudeck(self.config.get()).get("root") or ""
        cemu_keys: dict[str, Any] | None = None
        if mapping.get("launcher") == "cemu":
            cemu_keys = cemu_keys_status(emu_root)
            if not dry_run:
                cemu_keys = sync_cemu_keys(emu_root)
                self.log(
                    f"cemu keys {cemu_keys.get('status')} "
                    f"({cemu_keys.get('target_keys', 0)} keys)"
                )
        if dry_run:
            result = {"dry_run": True, "command": command, "rom": rom_id, "path": installed["path"]}
            if cemu_keys is not None:
                result["cemu_keys"] = cemu_keys
            return result
        if not os.path.exists(command[0]):
            raise HttpError(409, f"The {mapping.get('launcher_name') or 'emulator'} launcher is missing.")
        # A launch must not start on a stale save: the game's saves are brought
        # in step with RomM first, and a save that cannot be restored or that
        # changed on both sides stops the launch instead of picking a winner.
        sync: dict[str, Any] | None = None
        try:
            sync = self.save_sync.before_play(rom, mapping, emu_root, self.client(), self.active_server())
        except SaveSyncError as exc:
            raise HttpError(
                409,
                str(exc),
                rom_id=rom_id,
                save_sync=exc.summary,
                save_conflict=exc.summary.get("status") == "conflict",
            ) from None
        log = os.path.join(data_dir(), "launcher.log")
        process = spawn_process(command, log)
        self.state.mark_played(rom, rom.get("platform_fs_slug") or mapping.get("platform") or "")
        note = f" after save sync ({sync.get('status')})" if sync else ""
        self.log(f"play rom {rom_id} with {command[0]} pid={process.pid}{note}")
        if sync and sync.get("enabled"):
            self.save_sync.watch_exit(process, rom, mapping, emu_root, self.client(), self.active_server())
        return {
            "launched": True,
            "pid": process.pid,
            "command": command,
            "path": installed["path"],
            "launcher": mapping.get("launcher"),
            "core": mapping.get("core"),
            "log": log,
            "saves": sync,
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
        if PIL_AVAILABLE:
            normalized = _normalize_logo_tile(data)
            if normalized:
                data, content_type = normalized, "image/png"
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
        limit = 64 * 1024 * 1024 if parsed.path == "/api/firmware/upload" else MAX_BODY
        if length > limit:
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
def _public_mapping(mapping: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    """The mapping fields the browser may rely on; add new keys here once."""
    return {
        "platform": mapping.get("platform") or "",
        "enabled": bool(mapping.get("enabled", enabled)),
        "dir": mapping.get("dir") or "",
        "path": mapping.get("path") or "",
        "exists": bool(mapping.get("exists")),
        "launcher": mapping.get("launcher") or "",
        "launcher_name": mapping.get("launcher_name") or "",
        "launcher_path": mapping.get("launcher_path") or "",
        "launcher_ready": bool(mapping.get("launcher_ready")),
        "launcher_url": mapping.get("launcher_url") or "",
        "bios_required": list(mapping.get("bios_required") or []),
        "bios_missing": list(mapping.get("bios_missing") or []),
        "bios_status": [dict(entry) for entry in (mapping.get("bios_status") or [])],
        "bios_folder": mapping.get("bios_folder") or "",
        "storage_status": [dict(entry) for entry in (mapping.get("storage_status") or [])],
        "storage_missing": list(mapping.get("storage_missing") or []),
        "storage_folder": mapping.get("storage_folder") or "",
        "core": mapping.get("core") or "",
        "source": mapping.get("source") or "",
        "ready": bool(mapping.get("exists") and mapping.get("launcher")),
    }


def _trim_rom(rom: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields the shelf needs and drop nested file trees.

    The RomM list endpoint with files enabled can return every patch and
    sidecar for a game; the shelf only needs the name of the top-level file,
    so trimming here keeps a 300-game library small in memory.
    """
    out = dict(rom)
    files = rom.get("files") or []
    top = [f for f in files if f.get("is_top_level")]
    out["files"] = [
        {key: entry.get(key) for key in ("file_name", "file_size", "is_top_level")}
        for entry in top
    ]
    return out


_ROM_FORMAT_SCORES = {
    ".chd": 6, ".iso": 6, ".rvz": 6, ".gcm": 6, ".wbfs": 6, ".wia": 6,
    ".3ds": 6, ".cci": 6, ".cia": 5, ".nsp": 6, ".xci": 6, ".nca": 5,
    ".7z": 2, ".zip": 2, ".rar": 1,
}


def _rom_extension(rom: dict[str, Any]) -> str:
    files = rom.get("files") or []
    top = [f for f in files if f.get("is_top_level")] or files
    for entry in top:
        name = str(entry.get("file_name") or "")
        extension = os.path.splitext(name)[1].lower()
        if extension:
            return extension
    return ""


def _rom_name_key(rom: dict[str, Any]) -> str:
    """Match sibling files by title, ignoring punctuation and region tags."""
    name = str(rom.get("name") or rom.get("fs_name_no_ext") or rom.get("fs_name") or "")
    name = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name)
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def _rom_preference(rom: dict[str, Any]) -> tuple[int, int, int, int]:
    """Lower wins: a main sibling first, then a directly launchable image, then size/id."""
    return (
        0 if rom.get("is_main_sibling") else 1,
        -_ROM_FORMAT_SCORES.get(_rom_extension(rom), 3),
        -int(rom.get("fs_size_bytes") or 0),
        -int(rom.get("id") or 0),
    )


_SERIAL_RE = re.compile(
    r"\b(S[CL]US|S[CL]PS|SLES|S[CL]PM|S[CL]KA|NUS|DOL|GALE|RVL|CTR)[-_ ]?\d{3,5}\b",
    re.IGNORECASE,
)


def _file_stem(rom: dict[str, Any]) -> str:
    files = rom.get("files") or []
    top = [f for f in files if f.get("is_top_level")] or files
    if not top:
        return ""
    stem = os.path.splitext(os.path.basename(str(top[0].get("file_name") or "")))[0]
    return re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", stem).strip()


def _stems_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two sibling files look like the same game, not a bad IGDB match.

    A serial filename (SCUS-97101) counts as the same game as its title.  Otherwise
    compare the filename words: remake/metadata mis-matches such as
    "Super Mario All-Stars" vs "Super Mario RPG" share too few words to merge.
    """
    left_stem = _file_stem(left)
    right_stem = _file_stem(right)
    if not left_stem or not right_stem:
        return False
    if _SERIAL_RE.search(left_stem) or _SERIAL_RE.search(right_stem):
        return True
    left_words = set(re.findall(r"[a-z0-9]+", left_stem.casefold()))
    right_words = set(re.findall(r"[a-z0-9]+", right_stem.casefold()))
    if not left_words or not right_words:
        return False
    return len(left_words & right_words) / len(left_words | right_words) >= 0.5


def collapse_sibling_roms(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark alternate files of the same game and pick one visible representative.

    RomM links files that matched the same IGDB entry as siblings, but that match
    can be wrong (Super Mario All-Stars is a sibling of Super Mario RPG).  Only
    files whose titles normalise to the same key collapse; the rest stay separate.
    Hidden siblings remain in the list so installs and detail pages still resolve.
    """
    by_id = {int(rom["id"]): rom for rom in items if rom.get("id") is not None}
    parent = {rom_id: rom_id for rom_id in by_id}

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    for rom_id, rom in by_id.items():
        for sibling in rom.get("sibling_roms") or []:
            sibling_id = int(sibling.get("id") or 0)
            if sibling_id in by_id:
                union(rom_id, sibling_id)

    groups: dict[int, list[dict[str, Any]]] = {}
    for rom_id, rom in by_id.items():
        groups.setdefault(find(rom_id), []).append(rom)

    for group in groups.values():
        buckets: dict[str, list[dict[str, Any]]] = {}
        for rom in group:
            buckets.setdefault(_rom_name_key(rom), []).append(rom)
        for same_name in buckets.values():
            clusters: list[list[dict[str, Any]]] = []
            for rom in same_name:
                for cluster in clusters:
                    if any(_stems_compatible(rom, other) for other in cluster):
                        cluster.append(rom)
                        break
                else:
                    clusters.append([rom])
            for cluster in clusters:
                if len(cluster) < 2:
                    continue
                representative = min(cluster, key=_rom_preference)
                versions = [
                    {
                        "id": rom.get("id"),
                        "name": rom.get("name"),
                        "file_name": (rom.get("files") or [{}])[0].get("file_name") or "",
                        "size": int(rom.get("fs_size_bytes") or 0),
                    }
                    for rom in sorted(cluster, key=_rom_preference)
                ]
                for rom in cluster:
                    rom["version_count"] = len(cluster)
                    rom["versions"] = versions
                    if rom is not representative:
                        rom["hidden_sibling"] = True
                        rom["version_of"] = representative.get("id")
    return items


def _is_game_rom(rom: dict[str, Any]) -> bool:
    """Whether a RomM entry is a game rather than an emulator/tool artefact."""
    platform = str(rom.get("platform_fs_slug") or rom.get("platform_slug") or "").lower()
    if platform in TOOL_PLATFORMS:
        return False
    files = rom.get("files") or []
    if not files:
        return False
    top = [f for f in files if f.get("is_top_level")]
    if not top:
        return False
    for entry in top:
        name = str(entry.get("file_name") or "")
        extension = os.path.splitext(name)[1].lower()
        if extension in SAVE_FILE_EXTENSIONS:
            continue
        if extension and extension not in TOOL_EXTENSIONS:
            return True
    return False


def _find_emudeck_installer() -> str:
    """Find the EmuDeck AppImage, the place emulators are installed from."""
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, "Applications", "EmuDeck*.AppImage"),
        os.path.join(home, "Emulation", "tools", "EmuDeck*.AppImage"),
        os.path.join(home, ".local", "bin", "EmuDeck*.AppImage"),
    ]
    for pattern in patterns:
        for path in sorted(glob.glob(pattern), reverse=True):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return ""


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


def _normalize_logo_tile(data: bytes) -> bytes | None:
    """Trim a platform logo's backing and fit it on a transparent 320x120 tile.

    IGDB's platform images are a mix of black-backed boxes, white-backed
    labels and wide wordmarks.  Flood-filling the corner colour turns those
    into one consistent, readable mark; dark logos are lifted so they do not
    disappear on the dark card.
    """
    if not PIL_AVAILABLE:
        return None
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    # Work at card size: 1900px-wide IGDB art gains nothing at 288px output and
    # makes the flood fill an order of magnitude slower.
    image.thumbnail((520, 520), Image.LANCZOS)
    width, height = image.size
    if width < 8 or height < 8:
        return None
    patch = max(4, min(width, height) // 16)
    corners = [
        image.crop((0, 0, patch, patch)),
        image.crop((width - patch, 0, width, patch)),
        image.crop((0, height - patch, patch, height)),
        image.crop((width - patch, height - patch, width, height)),
    ]
    medians = [ImageStat.Stat(part).median for part in corners]
    background = tuple(int(sum(m[i] for m in medians) / len(medians)) for i in range(3))
    neutral = max(background) - min(background) <= 12
    dark = max(background) < 60
    light = min(background) > 195
    mid = 90 <= max(background) <= 175
    if not (neutral and (dark or light or mid)):
        # A coloured backdrop is part of the artwork; leave it alone.
        return None
    marker = (255, 0, 255)
    work = image.copy()
    seeds = [
        (0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1),
        (width // 2, 0), (width // 2, height - 1), (0, height // 2), (width - 1, height // 2),
    ]
    for point in seeds:
        try:
            ImageDraw.floodfill(work, point, marker, thresh=16)
        except Exception:
            continue
    marker_img = Image.new("RGB", image.size, marker)
    difference = ImageChops.difference(work, marker_img).convert("L")
    background_mask = difference.point(lambda value: 255 if value == 0 else 0)
    alpha = ImageOps.invert(background_mask)
    if alpha.getbbox() is None or ImageStat.Stat(alpha).sum[0] < width * height * 8:
        return None
    rgba = image.convert("RGBA")
    rgba.putalpha(alpha)
    # A large interior white plate (the Arcade badge) reads as a hot square on
    # a dark card; drop it when the outside was not already white.
    if not light:
        near_white = image.convert("L").point(lambda value: 255 if value >= 228 else 0)
        white_ratio = ImageStat.Stat(near_white).sum[0] / (width * height * 255)
        if white_ratio > (0.30 if mid else 0.55):
            rgba.putalpha(ImageChops.multiply(rgba.getchannel("A"), ImageOps.invert(near_white)))
    bbox = rgba.getbbox()
    if bbox:
        rgba = rgba.crop(bbox)
    grey = rgba.convert("L")
    stats = ImageStat.Stat(grey, mask=rgba.getchannel("A"))
    mean = (stats.mean[0] / 255) if stats.count[0] else 0
    if mean and mean < 0.44:
        rgba = ImageEnhance.Brightness(rgba).enhance(min(3.4, 0.58 / mean))
    rgba.thumbnail((288, 96), Image.LANCZOS)
    tile = Image.new("RGBA", (320, 120), (0, 0, 0, 0))
    tile.alpha_composite(rgba, ((320 - rgba.width) // 2, (120 - rgba.height) // 2))
    out = io.BytesIO()
    tile.save(out, format="PNG", optimize=True)
    return out.getvalue()


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
