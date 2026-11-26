"""Keep EmuDeck saves in step with RomM around a launch.

RomM pairs saves on ``(rom_id, slot)``: a client that sends the same slot name
for a game keeps one save in sync across machines.  RomMArcade uses
``autosave`` for the game's own save and ``autosave-<ext>`` for sidecars such
as an N64 ``.rtc`` clock file, the same convention RommDeck uses, so the two
clients meet on the same slot rather than fighting over separate ones.

Before a launch the service pulls whatever the server holds that this machine
does not, and pushes local progress that the server is missing; the emulator
is not started until the pull is in place.  After the emulator exits the save
it wrote is pushed back.  A save that changed on both sides is a conflict:
the launch stops and says so, because silently picking a winner is how a
save gets lost.

Everything here is stdlib; the HTTP shapes live in ``romm.RomMClient``.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import urllib.parse
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import CLIENT_ID, __version__
from .config import data_dir
from .emudeck import matches_rom_stem, rom_target_name
from .romm import RomMError

SLOT = "autosave"
SIDECAR_PREFIX = "autosave-"
# Extensions an emulator writes as its battery save.  When several files belong
# to one game the one from this list is the shared slot; the rest travel as
# sidecars so a clock file cannot overwrite the save itself.
MAIN_SAVE_EXTENSIONS = {
    ".srm", ".sav", ".sra", ".fla", ".eep", ".nv", ".mpk", ".mcd", ".ps2",
    ".bcr", ".bks", ".crv", ".dci", ".gci", ".raw", ".rtc.dat",
}
MAX_UPLOAD = 64 * 1024 * 1024
CLOCK_SKEW_FLUSH = 2.0


class SaveSyncError(Exception):
    """A sync that must stop the launch, with the summary the UI can show."""

    def __init__(self, message: str, summary: dict[str, Any]):
        super().__init__(message)
        self.summary = summary


class SaveSyncUnsupported(Exception):
    """The server has no save-sync API; playing stays possible."""


@dataclass
class LocalSave:
    """One local save unit: a file, or a folder the emulator writes as a save."""

    path: str
    name: str
    slot: str = ""
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0
    hash: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _md5_file(path: str) -> str | None:
    try:
        digest = hashlib.md5(usedforsecurity=False)
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def folder_hash(path: str) -> str | None:
    """Hash a save folder the way RomM hashes a save zip.

    RomM stores a folder save as a zip and hashes it as md5 of the sorted
    ``name:entry-md5`` lines, so the same folder hashes identically here and on
    the server and an unchanged upload stays a no-op.
    """
    lines: list[str] = []
    try:
        for base, _dirs, files in os.walk(path):
            for name in files:
                full = os.path.join(base, name)
                relative = os.path.relpath(full, path).replace(os.sep, "/")
                digest = _md5_file(full)
                if digest is None:
                    return None
                lines.append(f"{relative}:{digest}")
    except OSError:
        return None
    if not lines:
        return None
    lines.sort()
    return _md5_bytes("\n".join(lines).encode())


def _newest_mtime(path: str) -> float:
    try:
        if os.path.isfile(path):
            return os.path.getmtime(path)
        newest = 0.0
        for base, _dirs, files in os.walk(path):
            for name in files:
                newest = max(newest, os.path.getmtime(os.path.join(base, name)))
        return newest or os.path.getmtime(path)
    except OSError:
        return 0.0


def _size_of(path: str) -> int:
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for base, _dirs, files in os.walk(path):
            for name in files:
                total += os.path.getsize(os.path.join(base, name))
        return total
    except OSError:
        return 0


def _slot_for_extension(extension: str, slot: str = SLOT) -> str:
    cleaned = "".join(ch for ch in extension.lower().lstrip(".") if ch.isalnum() or ch in "-_")
    return f"{slot}-{cleaned or 'extra'}"


def rom_stem(rom: dict[str, Any]) -> str:
    """The name the emulator gives this game's save, without its extension."""
    return os.path.splitext(rom_target_name(rom))[0]


def save_root(emulation_root: str, platform_dir: str) -> str:
    if not emulation_root or not platform_dir:
        return ""
    return os.path.join(emulation_root, "saves", platform_dir)


def local_saves(root: str, stem: str, slot: str = SLOT) -> list[LocalSave]:
    """Every save belonging to one game in EmuDeck's saves folder.

    Only immediate children named after the ROM are taken: memory cards and
    shared folders that serve a whole system are not this game's to sync.
    """
    if not root or not stem or not os.path.isdir(root):
        return []
    files: list[LocalSave] = []
    folders: list[LocalSave] = []
    for name in sorted(os.listdir(root)):
        if not matches_rom_stem(name, stem):
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path):
            folders.append(LocalSave(path=path, name=name, slot=slot, is_dir=True,
                                     size=_size_of(path), mtime=_newest_mtime(path),
                                     hash=folder_hash(path)))
        elif os.path.isfile(path):
            files.append(LocalSave(path=path, name=name, size=_size_of(path),
                                   mtime=_newest_mtime(path), hash=_md5_file(path)))
    out: list[LocalSave] = []
    main: LocalSave | None = None
    if folders and not files:
        main = folders[0]
        out.extend(folders[1:])
    elif files:
        preferred = [f for f in files if os.path.splitext(f.name)[1].lower() in MAIN_SAVE_EXTENSIONS]
        main = max(preferred or files, key=lambda f: (f.size, f.name))
        for extra in files:
            if extra is main:
                continue
            extra.slot = _slot_for_extension(os.path.splitext(extra.name)[1], slot)
            out.append(extra)
    if main is not None:
        main.slot = slot
        out.insert(0, main)
    for save in out:
        save.extra["mtime_iso"] = datetime.fromtimestamp(save.mtime, timezone.utc).isoformat()
    return out


class SyncState:
    """What this machine last agreed with the server, per server/ROM/slot."""

    def __init__(self, path: str | None = None):
        self._path = path or os.path.join(data_dir(), "save-sync.json")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "saves": {}, "slots": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("saves"), dict):
                self._data = {
                    "version": 1,
                    "saves": data["saves"],
                    "slots": data.get("slots") if isinstance(data.get("slots"), dict) else {},
                }
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        temp = self._path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp, self._path)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._data["saves"].get(key)
            return dict(entry) if isinstance(entry, dict) else None

    def put(self, key: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._data["saves"][key] = record
            self._save()

    def drop(self, key: str) -> None:
        with self._lock:
            if self._data["saves"].pop(key, None) is not None:
                self._save()

    def slot_for(self, key: str) -> str:
        with self._lock:
            return str(self._data["slots"].get(key) or "")

    def set_slot(self, key: str, slot: str) -> None:
        with self._lock:
            if slot:
                self._data["slots"][key] = slot
            else:
                self._data["slots"].pop(key, None)
            self._save()


class SaveSyncService:
    """RomM save sync for one RomMArcade installation."""

    def __init__(self, config, log, client_device_identifier: str = ""):
        self.config = config
        self.log = log
        self.client_device_identifier = client_device_identifier or ""
        self.state = SyncState()
        self._lock = threading.RLock()
        self._last: dict[str, Any] = {}
        self._unsupported: set[str] = set()

    # -- settings ---------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        section = self.config.get().get("save_sync") or {}
        return {
            "enabled": bool(section.get("enabled", True)),
            "play_gate": bool(section.get("play_gate", True)),
            "conflicts": str(section.get("conflicts") or "block").lower(),
            "devices": section.get("devices") or {},
        }

    def snapshot(self) -> dict[str, Any]:
        settings = self.settings()
        return {
            "enabled": settings["enabled"],
            "play_gate": settings["play_gate"],
            "conflicts": settings["conflicts"],
            "last": dict(self._last),
        }

    # -- identity ---------------------------------------------------------
    def _server_key(self, server: dict[str, Any] | None) -> str:
        return str((server or {}).get("id") or (server or {}).get("url") or "")

    def _store_identity(self, server: dict[str, Any] | None, record: dict[str, Any]) -> None:
        key = self._server_key(server)
        if not key:
            return
        self.config.update({"save_sync": {"devices": {key: record}}})

    def identity(self, client, server: dict[str, Any] | None) -> dict[str, Any]:
        """The device this install syncs as.

        A paired (device-bound) token needs no id: the server infers the device
        from the token.  A personal token answers 400 and gets a device of its
        own, remembered in config so later launches stay cheap.
        """
        settings = self.settings()
        key = self._server_key(server)
        record = (settings.get("devices") or {}).get(key)
        if isinstance(record, dict) and (record.get("mode") == "token" or record.get("device_id")):
            return record
        try:
            client.sync_negotiate({"saves": [], "rom_ids": []})
        except RomMError as exc:
            detail = _detail_text(exc)
            if exc.status == 400 and "device_id" in detail:
                device_id = self._register_device(client)
                record = {"mode": "explicit", "device_id": device_id}
            elif _unsupported_error(exc):
                raise SaveSyncUnsupported() from None
            else:
                raise
        else:
            record = {"mode": "token", "device_id": ""}
        self._store_identity(server, record)
        return record

    def _register_device(self, client) -> str:
        payload = {
            "name": socket.gethostname(),
            "platform": _platform_name(),
            "client": CLIENT_ID,
            "client_version": __version__,
            "sync_mode": "api",
            "allow_existing": True,
        }
        if self.client_device_identifier:
            payload["client_device_identifier"] = self.client_device_identifier
        data = client.register_device(payload)
        device_id = str((data or {}).get("device_id") or (data or {}).get("id") or "")
        if not device_id:
            raise SaveSyncError(
                "The RomM server did not name the device it registered for save sync.",
                {"enabled": True, "status": "error"},
            )
        self.log(f"save sync: registered device {device_id} on RomM")
        return device_id

    # -- sync -------------------------------------------------------------
    def before_play(self, rom: dict[str, Any], mapping: dict[str, Any],
                    emulation_root: str, client, server: dict[str, Any] | None) -> dict[str, Any]:
        """Bring the game's saves in step before the emulator starts."""
        settings = self.settings()
        summary: dict[str, Any] = {
            "enabled": settings["enabled"],
            "status": "skipped",
            "downloaded": 0,
            "uploaded": 0,
            "conflicts": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
        }
        if not settings["enabled"]:
            return summary
        server_key = self._server_key(server)
        if server_key in self._unsupported:
            summary["status"] = "unsupported"
            summary["reason"] = "This RomM server has no save-sync API."
            return summary
        root = save_root(emulation_root, str(mapping.get("dir") or mapping.get("platform") or ""))
        stem = rom_stem(rom)
        if not root or not stem:
            summary["reason"] = "No save folder is known for this platform."
            return summary
        active_slot = self.active_slot(server_key, int(rom["id"]))
        summary["slot"] = active_slot
        with self._lock:
            try:
                saves = local_saves(root, stem, active_slot)
                summary["root"] = root
                try:
                    identity = self.identity(client, server)
                except SaveSyncUnsupported:
                    self._unsupported.add(server_key)
                    summary["status"] = "unsupported"
                    summary["reason"] = "This RomM server has no save-sync API."
                    return summary
                summary["device"] = identity.get("device_id") or "token"
                payload: dict[str, Any] = {
                    "saves": [_client_state(rom, save) for save in saves],
                    "rom_ids": [int(rom["id"])],
                }
                if identity.get("device_id"):
                    payload["device_id"] = identity["device_id"]
                try:
                    negotiated = client.sync_negotiate(payload)
                except RomMError as exc:
                    if _unsupported_error(exc):
                        self._unsupported.add(server_key)
                        summary["status"] = "unsupported"
                        summary["reason"] = "This RomM server has no save-sync API."
                        return summary
                    raise
                operations = [
                    op for op in (negotiated or {}).get("operations") or []
                    if int(op.get("rom_id") or 0) == int(rom["id"])
                ]
                os.makedirs(root, exist_ok=True)
                plan = self._plan(saves, operations, server_key, int(rom["id"]), active_slot)
                downloads = [item for item in plan if item["do"] == "download"]
                uploads = [item for item in plan if item["do"] == "upload"]
                conflicts = [item for item in plan if item["do"] == "conflict"]

                if conflicts:
                    summary["conflicts"] = len(conflicts)
                    summary["items"] = [_item_public(item) for item in plan]
                    summary["status"] = "conflict"
                    summary["reason"] = conflicts[0].get("reason") or ""
                    message = (
                        f"Your save for {rom.get('name') or 'this game'} changed here and on the server. "
                        "Open RomM to choose which copy to keep, then play again."
                    )
                    self.log(f"save sync: conflict for rom {rom['id']} ({conflicts[0].get('slot')})")
                    if settings["play_gate"]:
                        raise SaveSyncError(message, summary)

                for item in downloads:
                    try:
                        self._download(client, server_key, int(rom["id"]), item, identity, root, stem)
                        summary["downloaded"] += 1
                        summary["items"].append(_item_public(item))
                    except (RomMError, OSError, zipfile.BadZipFile) as exc:
                        summary["failed"] += 1
                        summary["status"] = "error"
                        summary["reason"] = f"Could not restore the server save: {exc}"
                        self.log(f"save sync: download failed for rom {rom['id']}: {exc}")
                        if settings["play_gate"]:
                            raise SaveSyncError(
                                f"{rom.get('name') or 'This game'}'s save could not be restored from RomM ({exc}). "
                                "The emulator was not started.",
                                summary,
                            ) from None
                for item in uploads:
                    try:
                        self._upload(client, server_key, int(rom["id"]), item, identity, mapping,
                                     overwrite=bool(item.get("overwrite")))
                        summary["uploaded"] += 1
                        summary["items"].append(_item_public(item))
                    except RomMError as exc:
                        if exc.status == 409:
                            summary["conflicts"] += 1
                            summary["status"] = "conflict"
                            summary["reason"] = "The server copy changed while syncing."
                            self.log(f"save sync: upload conflict for rom {rom['id']}")
                            if settings["play_gate"]:
                                raise SaveSyncError(
                                    f"{rom.get('name') or 'This game'}'s save changed on the server while syncing. "
                                    "Open RomM to choose which copy to keep, then play again.",
                                    summary,
                                ) from None
                        else:
                            summary["failed"] += 1
                            summary["reason"] = f"The local save could not be uploaded: {exc}"
                            self.log(f"save sync: upload failed for rom {rom['id']}: {exc}")
                summary["skipped"] = sum(1 for item in plan if item["do"] in ("skip", "noop"))
                if summary["status"] not in ("conflict", "error"):
                    summary["status"] = "synced"
                self._last = summary
                return summary
            except SaveSyncError:
                raise
            except RomMError as exc:
                summary["status"] = "error"
                summary["reason"] = str(exc)
                self._last = summary
                self.log(f"save sync: rom {rom.get('id')} failed: {exc}")
                if settings["play_gate"]:
                    raise SaveSyncError(
                        f"Saves did not sync ({exc}). The emulator was not started.",
                        summary,
                    ) from None
                return summary

    def after_play(self, rom: dict[str, Any], mapping: dict[str, Any],
                   emulation_root: str, client, server: dict[str, Any] | None) -> dict[str, Any]:
        """Push what the emulator wrote once it has exited."""
        settings = self.settings()
        summary: dict[str, Any] = {"enabled": settings["enabled"], "status": "skipped",
                                   "uploaded": 0, "failed": 0, "items": []}
        if not settings["enabled"]:
            return summary
        server_key = self._server_key(server)
        if server_key in self._unsupported:
            return summary
        root = save_root(emulation_root, str(mapping.get("dir") or mapping.get("platform") or ""))
        stem = rom_stem(rom)
        if not root or not stem:
            return summary
        active_slot = self.active_slot(server_key, int(rom["id"]))
        with self._lock:
            try:
                try:
                    identity = self.identity(client, server)
                except SaveSyncUnsupported:
                    self._unsupported.add(server_key)
                    summary["status"] = "unsupported"
                    return summary
                summary["device"] = identity.get("device_id") or "token"
                for save in local_saves(root, stem, active_slot):
                    item = {"do": "upload", "slot": save.slot, "local": save, "reason": "played"}
                    try:
                        self._upload(client, server_key, int(rom["id"]), item, identity, mapping, overwrite=True)
                        summary["uploaded"] += 1
                        summary["items"].append(_item_public(item))
                    except RomMError as exc:
                        if exc.status == 413:
                            summary["skipped"] = summary.get("skipped", 0) + 1
                            continue
                        summary["failed"] += 1
                        self.log(f"save sync: post-play upload failed for rom {rom['id']}: {exc}")
                summary["status"] = "synced"
            except RomMError as exc:
                summary["status"] = "error"
                summary["reason"] = str(exc)
                self.log(f"save sync: post-play sync for rom {rom.get('id')} failed: {exc}")
            self._last = summary
            return summary

    # -- management -------------------------------------------------------
    def active_slot(self, server_key: str, rom_id: int) -> str:
        slot = self.state.slot_for(f"{server_key}|{rom_id}")
        return slot or SLOT

    def set_active_slot(self, server: dict[str, Any] | None, rom_id: int, slot: str) -> str:
        cleaned = _clean_slot(slot)
        if not cleaned:
            raise SaveSyncError("A sync slot needs a name made of letters, digits, dots, dashes or underscores.",
                                {"status": "error"})
        self.state.set_slot(f"{self._server_key(server)}|{int(rom_id)}", cleaned)
        self.log(f"save sync: rom {rom_id} syncs slot {cleaned}")
        return cleaned

    def saves_for(self, rom: dict[str, Any], mapping: dict[str, Any],
                  emulation_root: str, client, server: dict[str, Any] | None) -> dict[str, Any]:
        """Server saves (every version), local saves and the active slot."""
        settings = self.settings()
        server_key = self._server_key(server)
        rom_id = int(rom["id"])
        slot = self.active_slot(server_key, rom_id)
        root = save_root(emulation_root, str(mapping.get("dir") or mapping.get("platform") or ""))
        stem = rom_stem(rom)
        out: dict[str, Any] = {
            "rom_id": rom_id,
            "slot": slot,
            "supported": True,
            "sync": self.snapshot(),
            "server": [],
            "slots": [],
            "local": [],
            "playing": "",
            "root": root,
        }
        local = local_saves(root, stem, slot) if root else []
        main = local[0] if local else None
        out["playing"] = main.name if main else ""
        out["local"] = [
            {
                "name": save.name,
                "slot": save.slot,
                "size": save.size,
                "mtime": save.extra.get("mtime_iso") or "",
                "hash": save.hash or "",
                "dir": save.is_dir,
                "main": save is main,
                "path": save.path,
            }
            for save in local
        ]
        try:
            identity = self.identity(client, server)
            out["device"] = identity.get("device_id") or "token"
            rows = client.saves(rom_id=rom_id) or []
        except SaveSyncUnsupported:
            self._unsupported.add(server_key)
            out["supported"] = False
            out["reason"] = "This RomM server has no save-sync API."
            return out
        except RomMError as exc:
            if _unsupported_error(exc):
                self._unsupported.add(server_key)
                out["supported"] = False
                out["reason"] = "This RomM server has no save-sync API."
                return out
            raise
        rows = sorted(rows, key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        newest: dict[str, Any] = {}
        for row in rows:
            key = row.get("slot")
            if key and key not in newest:
                newest[key] = row.get("id")
        for row in rows:
            item = _save_public(row)
            item["current"] = bool(row.get("slot")) and newest.get(row.get("slot")) == row.get("id")
            item["matches_local"] = bool(main and main.hash and row.get("content_hash") == main.hash)
            out["server"].append(item)
        slots: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("slot") or "")
            entry = slots.setdefault(key, {"slot": row.get("slot"), "count": 0, "latest": None})
            entry["count"] += 1
            if entry["latest"] is None:
                entry["latest"] = row.get("id")
        out["slots"] = sorted(slots.values(), key=lambda entry: (entry["slot"] is None, entry["slot"] or ""))
        return out

    def restore_save(self, rom: dict[str, Any], mapping: dict[str, Any], emulation_root: str,
                     client, server: dict[str, Any] | None, save_id: int) -> dict[str, Any]:
        """Write one server save here and make it this game's synced save."""
        server_key = self._server_key(server)
        rom_id = int(rom["id"])
        root = save_root(emulation_root, str(mapping.get("dir") or mapping.get("platform") or ""))
        stem = rom_stem(rom)
        slot = self.active_slot(server_key, rom_id)
        if not root or not stem:
            raise SaveSyncError("No save folder is known for this platform.", {"status": "error"})
        os.makedirs(root, exist_ok=True)
        save = client.save(int(save_id)) or {}
        if int(save.get("rom_id") or 0) != rom_id:
            raise SaveSyncError("That save belongs to a different game.", {"status": "error"})
        identity = self.identity(client, server)
        response = client.open_save_content(
            int(save_id), device_id=identity.get("device_id") or "", optimistic=False
        )
        try:
            content = response.read(MAX_UPLOAD + 1)
            content_type = (response.headers.get("Content-Type") or "").lower()
        finally:
            response.close()
        if len(content) > MAX_UPLOAD:
            raise SaveSyncError("That save is larger than RomMArcade will restore.", {"status": "error"})
        extension = os.path.splitext(str(save.get("file_name") or ""))[1]
        backup = ""
        if content[:4] == b"PK\x03\x04" or "zip" in content_type:
            target_dir = os.path.join(root, stem)
            if os.path.isdir(target_dir):
                backup = _backup_path(target_dir)
            _extract_zip(content, target_dir)
            local = LocalSave(path=target_dir, name=os.path.basename(target_dir), slot=slot,
                              is_dir=True, size=_size_of(target_dir), mtime=_newest_mtime(target_dir),
                              hash=folder_hash(target_dir))
        else:
            if not extension:
                extension = "." + slot[len(SIDECAR_PREFIX):] if slot.startswith(SIDECAR_PREFIX) else ".srm"
            target = os.path.join(root, stem + extension)
            if os.path.exists(target):
                backup = _backup_path(target)
            _write_atomic(target, content)
            try:
                stamp = _parse_time(save.get("updated_at"))
                if stamp > 0:
                    os.utime(target, (stamp, stamp))
            except OSError:
                pass
            local = LocalSave(path=target, name=os.path.basename(target), slot=slot,
                              size=_size_of(target), mtime=_newest_mtime(target),
                              hash=_md5_bytes(content))
        local.extra["mtime_iso"] = datetime.fromtimestamp(local.mtime, timezone.utc).isoformat()
        # Promote the chosen version into the sync slot, so the next launch on
        # this machine or another one starts from it instead of the old head.
        item = {"do": "upload", "slot": slot, "local": local, "reason": "restored"}
        self._upload(client, server_key, rom_id, item, identity, mapping, overwrite=True)
        self.state.set_slot(f"{server_key}|{rom_id}", slot)
        if identity.get("device_id"):
            try:
                client.confirm_save_downloaded(int(save_id), identity["device_id"],
                                                save.get("content_hash"))
            except RomMError:
                pass
        self.log(f"save sync: restored save {save_id} into {local.path}")
        return {
            "save_id": int(save_id),
            "slot": slot,
            "path": local.path,
            "bytes": len(content),
            "backup": backup,
            "uploaded": item.get("save_id"),
            "file": local.name,
        }

    def upload_local(self, rom: dict[str, Any], mapping: dict[str, Any], emulation_root: str,
                     client, server: dict[str, Any] | None, name: str) -> dict[str, Any]:
        """Upload one of this machine's saves as the game's synced save."""
        server_key = self._server_key(server)
        rom_id = int(rom["id"])
        root = save_root(emulation_root, str(mapping.get("dir") or mapping.get("platform") or ""))
        stem = rom_stem(rom)
        slot = self.active_slot(server_key, rom_id)
        clean = os.path.basename(str(name or ""))
        path = os.path.join(root, clean)
        if not clean or not root or os.path.commonpath([os.path.abspath(root), os.path.abspath(path)]) != os.path.abspath(root):
            raise SaveSyncError("That save is not in this game's save folder.", {"status": "error"})
        if not os.path.exists(path):
            raise SaveSyncError("That save file is no longer on this machine.", {"status": "error"})
        known = {save.path: save for save in local_saves(root, stem, slot)}
        local = known.get(path)
        if local is None:
            if not matches_rom_stem(clean, stem):
                raise SaveSyncError("That file does not belong to this game.", {"status": "error"})
            local = LocalSave(path=path, name=clean, slot=slot, is_dir=os.path.isdir(path),
                              size=_size_of(path), mtime=_newest_mtime(path),
                              hash=folder_hash(path) if os.path.isdir(path) else _md5_file(path))
        identity = self.identity(client, server)
        item = {"do": "upload", "slot": local.slot, "local": local, "reason": "manual upload"}
        self._upload(client, server_key, rom_id, item, identity, mapping, overwrite=True)
        self.state.set_slot(f"{server_key}|{rom_id}", local.slot)
        return {"slot": local.slot, "file": local.name, "save_id": item.get("save_id"),
                "bytes": item.get("bytes") or 0}

    def download_save(self, client, save_id: int) -> tuple[bytes, str, str]:
        """Bytes, media type and file name for one server save."""
        response = client.open_save_content(int(save_id), optimistic=False)
        try:
            data = response.read(MAX_UPLOAD + 1)
            content_type = response.headers.get("Content-Type") or "application/octet-stream"
            disposition = response.headers.get("Content-Disposition") or ""
        finally:
            response.close()
        if len(data) > MAX_UPLOAD:
            raise SaveSyncError("That save is larger than RomMArcade will download.", {"status": "error"})
        name = ""
        marker = "filename*=utf-8''"
        lowered = disposition.lower()
        if marker in lowered:
            name = urllib.parse.unquote(disposition[lowered.index(marker) + len(marker):].split(";")[0].strip().strip('"'))
        elif "filename=" in lowered:
            name = disposition[lowered.index("filename=") + 9:].split(";")[0].strip().strip('"')
        return data, content_type, name or f"save-{int(save_id)}.bin"

    def watch_exit(self, process, rom: dict[str, Any], mapping: dict[str, Any],
                   emulation_root: str, client, server: dict[str, Any] | None) -> None:
        """Upload the game's save once the emulator has closed."""
        if not self.settings()["enabled"]:
            return

        def run() -> None:
            try:
                process.wait()
            except Exception:  # noqa: BLE001 - the wait itself must never raise
                return
            time.sleep(CLOCK_SKEW_FLUSH)
            try:
                self.after_play(rom, mapping, emulation_root, client, server)
            except Exception as exc:  # noqa: BLE001 - a daemon thread must not die loudly
                self.log(f"save sync: post-play watcher failed for rom {rom.get('id')}: {exc}")

        threading.Thread(target=run, name=f"rommarcade-saves-{rom.get('id')}", daemon=True).start()

    # -- planning ---------------------------------------------------------
    def _plan(self, saves: list[LocalSave], operations: list[dict[str, Any]],
              server_key: str, rom_id: int, active_slot: str = SLOT) -> list[dict[str, Any]]:
        """Decide, per operation, what this launch must do.

        RomM decides by timestamps when it has no sync history for a device,
        which is how a save gets lost: a fresh install's starter save is
        "newer" than years of progress.  The local record of what was last
        agreed is the tie-breaker, and an unknown local file that would
        replace a different server save is a conflict, never an upload.
        """
        by_slot = {save.slot: save for save in saves}
        plan: list[dict[str, Any]] = []
        answered: set[str] = set()
        for op in operations:
            slot = op.get("slot")
            if not slot:
                continue
            local = by_slot.get(slot)
            answered.add(slot)
            action = str(op.get("action") or "no_op")
            server_hash = op.get("server_content_hash") or ""
            record = self.state.get(f"{server_key}|{rom_id}|{slot}")
            local_changed = bool(local) and (
                record is None or (record.get("local_hash") or "") != (local.hash or "")
            )
            if action == "no_op":
                if local and server_hash and local.hash != server_hash:
                    action = "upload" if local_changed else "download"
                else:
                    if local:
                        self.state.put(f"{server_key}|{rom_id}|{slot}", {
                            "local_hash": local.hash, "server_hash": server_hash or local.hash,
                            "save_id": op.get("save_id"), "path": local.path,
                            "synced_at": datetime.now(timezone.utc).isoformat(),
                        })
                    continue
            if action == "delete":
                plan.append({"do": "skip", "slot": slot, "local": local, "op": op,
                             "reason": "Removed on the server; left alone here."})
                continue
            if action == "download":
                if local and (record is None or local_changed):
                    plan.append({"do": "conflict", "slot": slot, "local": local, "op": op,
                                 "reason": "This machine's save and the server's both changed."})
                    continue
                if not local and not _slot_is_ours(slot, active_slot):
                    plan.append({"do": "skip", "slot": slot, "local": None, "op": op,
                                 "reason": "Another device's slot."})
                    continue
                plan.append({"do": "download", "slot": slot, "local": local, "op": op,
                             "reason": op.get("reason") or ""})
                continue
            if action == "upload":
                if local and record is None and server_hash and local.hash != server_hash:
                    plan.append({"do": "conflict", "slot": slot, "local": local, "op": op,
                                 "reason": "A save here was never synced and the server holds a different one."})
                    continue
                if not local:
                    plan.append({"do": "skip", "slot": slot, "local": None, "op": op,
                                 "reason": "The server expects a save this machine no longer has."})
                    continue
                plan.append({"do": "upload", "slot": slot, "local": local, "op": op,
                             "reason": op.get("reason") or ""})
                continue
            if action == "conflict":
                policy = self.settings()["conflicts"]
                if policy == "newest" and local:
                    server_at = _parse_time(op.get("server_updated_at"))
                    policy = "local" if (local.mtime >= server_at) else "server"
                if policy == "local" and local:
                    plan.append({"do": "upload", "slot": slot, "local": local, "op": op,
                                 "overwrite": True, "reason": "Conflict: keeping this machine's copy."})
                elif policy == "server":
                    plan.append({"do": "download", "slot": slot, "local": local, "op": op,
                                 "force": True, "reason": "Conflict: keeping the server copy."})
                else:
                    plan.append({"do": "conflict", "slot": slot, "local": local, "op": op,
                                 "reason": op.get("reason") or "Changed on both sides."})
                continue
            plan.append({"do": "skip", "slot": slot, "local": local, "op": op,
                         "reason": f"Unknown operation {action}."})
        # A local save the server never answered still has to go up, or the
        # server silently falls behind this machine.
        for save in saves:
            if save.slot in answered:
                continue
            plan.append({"do": "upload", "slot": save.slot, "local": save, "op": {},
                         "reason": "The server has no copy of this save."})
        return plan

    # -- transfers --------------------------------------------------------
    def _download(self, client, server_key: str, rom_id: int, item: dict[str, Any],
                  identity: dict[str, Any], root: str, stem: str) -> None:
        op = item.get("op") or {}
        save_id = int(op.get("save_id") or 0)
        if not save_id:
            raise SaveSyncError("The server offered no save to download.", {"status": "error"})
        slot = str(item.get("slot") or SLOT)
        local: LocalSave | None = item.get("local")
        if item.get("force") and local and not local.is_dir:
            backup = local.path + time.strftime(".pre-sync-%Y%m%d-%H%M%S")
            try:
                import shutil

                shutil.copy2(local.path, backup)
                self.log(f"save sync: kept this machine's conflicted save at {backup}")
            except OSError:
                pass
        response = client.open_save_content(
            save_id,
            device_id=identity.get("device_id") or "",
            optimistic=False,
        )
        try:
            content = response.read(MAX_UPLOAD + 1)
            content_type = (response.headers.get("Content-Type") or "").lower()
        finally:
            response.close()
        if len(content) > MAX_UPLOAD:
            raise SaveSyncError("That save is larger than RomMArcade will restore.", {"status": "error"})
        if not root:
            raise SaveSyncError("No save folder to restore into.", {"status": "error"})
        os.makedirs(root, exist_ok=True)
        if content[:4] == b"PK\x03\x04" or "zip" in content_type:
            target_dir = local.path if local and local.is_dir else os.path.join(root, stem)
            _extract_zip(content, target_dir)
            local_hash = folder_hash(target_dir)
            path = target_dir
        else:
            extension = os.path.splitext(str(op.get("file_name") or ""))[1]
            if slot.startswith(SIDECAR_PREFIX):
                extension = "." + slot[len(SIDECAR_PREFIX):]
            target = local.path if local and not local.is_dir else os.path.join(root, stem + (extension or ".srm"))
            _write_atomic(target, content)
            try:
                stamp = _parse_time(op.get("server_updated_at"))
                if stamp > 0:
                    os.utime(target, (stamp, stamp))
            except OSError:
                pass
            local_hash = _md5_bytes(content)
            path = target
        server_hash = op.get("server_content_hash") or local_hash or ""
        self.state.put(f"{server_key}|{rom_id}|{slot}", {
            "local_hash": local_hash, "server_hash": server_hash,
            "save_id": save_id, "path": path,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        })
        if identity.get("device_id"):
            try:
                client.confirm_save_downloaded(save_id, identity["device_id"], server_hash or None)
            except RomMError as exc:
                self.log(f"save sync: RomM did not confirm save {save_id}: {exc}")
        item["file"] = os.path.basename(path)
        item["bytes"] = len(content)
        self.log(f"save sync: downloaded save {save_id} ({slot}) -> {path}")

    def _upload(self, client, server_key: str, rom_id: int, item: dict[str, Any],
                identity: dict[str, Any], mapping: dict[str, Any], overwrite: bool) -> None:
        local: LocalSave | None = item.get("local")
        if not local:
            return
        if local.is_dir:
            data, file_name, content_hash = _zip_folder(local.path, local.name)
        else:
            size = os.path.getsize(local.path)
            if size > MAX_UPLOAD:
                raise RomMError("That save is larger than the server allows.", 413)
            with open(local.path, "rb") as handle:
                data = handle.read()
            file_name, content_hash = local.name, local.hash or _md5_bytes(data)
        emulator = str(mapping.get("core") or mapping.get("launcher") or "") or None
        result = client.upload_save(
            rom_id,
            data,
            file_name=file_name,
            emulator=emulator,
            slot=item.get("slot"),
            device_id=identity.get("device_id") or "",
            content_hash=content_hash,
            overwrite=bool(overwrite or item.get("overwrite")),
        )
        server_hash = str((result or {}).get("content_hash") or content_hash or "")
        self.state.put(
            f"{server_key}|{rom_id}|{item.get('slot')}",
            {
                "local_hash": local.hash,
                "server_hash": server_hash,
                "save_id": (result or {}).get("id"),
                "path": local.path,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        item["file"] = file_name
        item["bytes"] = len(data)
        self.log(f"save sync: uploaded {file_name} ({item.get('slot')}) for rom {rom_id}")


def _slot_is_ours(slot: str, active: str = SLOT) -> bool:
    return slot == active or slot.startswith(active + "-")


def _client_state(rom: dict[str, Any], save: LocalSave) -> dict[str, Any]:
    return {
        "rom_id": int(rom["id"]),
        "file_name": save.name,
        "slot": save.slot,
        "content_hash": save.hash,
        "updated_at": save.extra.get("mtime_iso") or datetime.fromtimestamp(save.mtime, timezone.utc).isoformat(),
        "file_size_bytes": save.size,
    }


def _item_public(item: dict[str, Any]) -> dict[str, Any]:
    local = item.get("local")
    out = {
        "action": item.get("do"),
        "slot": item.get("slot"),
        "file": item.get("file") or (local.name if local else ""),
        "reason": item.get("reason") or "",
    }
    if item.get("bytes"):
        out["bytes"] = item["bytes"]
    return out


def _clean_slot(slot: str) -> str:
    cleaned = "".join(ch for ch in str(slot or "").strip() if ch.isalnum() or ch in "._-")
    return cleaned[:64]


def _backup_path(path: str) -> str:
    import shutil

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path + f".pre-restore-{stamp}"
    try:
        if os.path.isdir(path):
            shutil.copytree(path, backup)
        else:
            shutil.copy2(path, backup)
        return backup
    except OSError:
        return ""


def _save_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "rom_id": row.get("rom_id"),
        "file_name": row.get("file_name") or "",
        "slot": row.get("slot"),
        "emulator": row.get("emulator") or "",
        "size": int(row.get("file_size_bytes") or 0),
        "updated_at": row.get("updated_at") or "",
        "created_at": row.get("created_at") or "",
        "content_hash": row.get("content_hash") or "",
        "favorite": bool(row.get("is_favorite")),
        "labels": row.get("labels") or [],
        "public": bool(row.get("is_public")),
        "missing": bool(row.get("missing_from_fs")),
    }


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".part"
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _extract_zip(content: bytes, target_dir: str) -> None:
    import io

    staging = target_dir + ".extracting"
    if os.path.isdir(staging):
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as bundle:
            for member in bundle.infolist():
                if member.is_dir():
                    continue
                name = member.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if name.startswith("/") or any(p == ".." for p in parts):
                    raise zipfile.BadZipFile("unsafe path in save archive")
                target = os.path.join(staging, *parts)
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with bundle.open(member) as source, open(target, "wb") as out:
                    out.write(source.read(MAX_UPLOAD))
    except Exception:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        raise
    os.makedirs(target_dir, exist_ok=True)
    import shutil

    for entry in os.listdir(staging):
        source = os.path.join(staging, entry)
        target = os.path.join(target_dir, entry)
        if os.path.isdir(source):
            if os.path.isdir(target):
                shutil.rmtree(target)
            shutil.move(source, target)
        else:
            os.replace(source, target)
    shutil.rmtree(staging, ignore_errors=True)


def _zip_folder(path: str, name: str) -> tuple[bytes, str, str | None]:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for base, _dirs, files in os.walk(path):
            for file_name in sorted(files):
                full = os.path.join(base, file_name)
                relative = os.path.relpath(full, path).replace(os.sep, "/")
                bundle.write(full, relative)
    data = buffer.getvalue()
    if len(data) > MAX_UPLOAD:
        raise RomMError("That save is larger than the server allows.", 413)
    return data, (name or "save") + ".zip", folder_hash(path)


def _parse_time(value: Any) -> float:
    if isinstance(value, (int, float)) and value:
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def _detail_text(exc: RomMError) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("detail") or detail.get("message") or "")
    if isinstance(detail, str):
        return detail
    return str(exc)


def _device_problem(exc: RomMError) -> bool:
    text = _detail_text(exc).lower()
    return "device" in text


def _unsupported_error(exc: RomMError) -> bool:
    """A 404/405 that is about the route itself, not about a device."""
    return exc.status in (404, 405) and not _device_problem(exc)


def _platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"
