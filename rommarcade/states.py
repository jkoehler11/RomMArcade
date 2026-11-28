"""Manual save-state exchange with RomM.

Save states are emulator-specific snapshots, not the game-owned battery save.
They are never pulled automatically before a launch: the emulator, core and
version that wrote a state must be the one that reads it.  This module only
lists what RomM has, copies a chosen state into EmuDeck's state folder, and
uploads a state that is already on this machine.

The UI is deliberately explicit about the copy step.  A saved state is a file
the emulator loads from its own menu; RomMArcade does not hot-load it into a
running game.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from typing import Any

from .emudeck import matches_rom_stem, save_roots
from .romm import RomMError
from .saves import MAX_UPLOAD, rom_stem

STATE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class StateError(Exception):
    """A state action that can be shown to the user."""


def state_root_for(emulation_root: str, mapping: dict[str, Any]) -> str:
    """The folder the mapped emulator reads save states from."""
    roots = save_roots(
        emulation_root,
        str(mapping.get("dir") or mapping.get("platform") or ""),
        str(mapping.get("launcher") or ""),
    )
    return roots[-1] if roots else ""


def _iso(value: Any) -> str:
    if not value:
        return ""
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return str(value)


def local_states(root: str, stem: str) -> list[dict[str, Any]]:
    """State files in one emulator's state folder that belong to this game."""
    if not root or not stem or not os.path.isdir(root):
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(os.listdir(root)):
        lowered = name.lower()
        if name.startswith(".") or lowered.endswith((".part", ".tmp", "~")):
            continue
        path = os.path.join(root, name)
        if not os.path.isfile(path) or not matches_rom_stem(name, stem):
            continue
        if os.path.splitext(name)[1].lower() in STATE_IMAGE_EXTENSIONS:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        out.append({
            "name": name,
            "path": path,
            "size": int(stat.st_size),
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    return out


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "rom_id": row.get("rom_id"),
        "file_name": row.get("file_name") or "",
        "emulator": row.get("emulator") or "",
        "core": row.get("core") or "",
        "size": int(row.get("file_size_bytes") or 0),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "favorite": bool(row.get("is_favorite")),
        "public": bool(row.get("is_public")),
        "labels": row.get("labels") or [],
        "missing": bool(row.get("missing_from_fs")),
    }


def list_states(
    rom: dict[str, Any],
    mapping: dict[str, Any],
    emulation_root: str,
    client,
    server: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the States tab needs for one game."""
    rom_id = int(rom["id"])
    root = state_root_for(emulation_root, mapping)
    stem = rom_stem(rom)
    out: dict[str, Any] = {
        "rom_id": rom_id,
        "root": root,
        "launcher": mapping.get("launcher") or "",
        "launcher_name": mapping.get("launcher_name") or mapping.get("launcher") or "",
        "supported": bool(root),
        "local": local_states(root, stem),
        "server": [],
    }
    if not root:
        out["reason"] = "No save-state folder is known for this platform."
        return out
    try:
        rows = client.states(rom_id=rom_id) or []
    except RomMError as exc:
        out["error"] = (
            "This RomM server has no save-state API."
            if exc.status == 404
            else str(exc)
        )
        return out
    except Exception as exc:  # pragma: no cover - defensive around a broken server
        out["error"] = str(exc) or "Save states could not be read from RomM."
        return out
    rows = sorted(rows, key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    local_names = {str(item.get("name") or "").casefold() for item in out["local"]}
    for index, row in enumerate(rows):
        item = _public(row)
        item["here"] = str(item.get("file_name") or "").casefold() in local_names
        if index == 0 and _active_state_name(rom, row).casefold() in local_names:
            # The newest server state may be installed under the emulator's
            # plain stem rather than RomM's tagged name.
            item["here"] = True
        out["server"].append(item)
    return out


def _active_state_name(rom: dict[str, Any], state: dict[str, Any]) -> str:
    """The name the emulator uses for this game's current state.

    RomM keeps a timestamp tag in a state's name so versions remain distinct;
    copying that name into the emulator's state folder would leave the state
    menu looking for the plain ROM stem.  Keep the extension, use the stem.
    """
    extension = os.path.splitext(str(state.get("file_name") or ""))[1].lower()
    if (
        not extension.startswith(".")
        or len(extension) > 13
        or not extension[1:].isalnum()
    ):
        extension = ".state"
    return rom_stem(rom) + extension


def _safe_name(value: str) -> str:
    name = os.path.basename(str(value or "").replace("\\", "/")).strip()
    name = name.replace("\x00", "")
    if not name or name in (".", ".."):
        raise StateError("RomM did not give this state a usable file name.")
    return name


def _backup(path: str) -> str:
    """Keep the previous state beside the state folder, not in the emulator's list."""
    folder = os.path.join(os.path.dirname(path), ".rommarcade", "backups")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = os.path.join(folder, f"{os.path.basename(path)}.pre-download-{stamp}")
    shutil.copy2(path, target)
    return target


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".part"
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            if os.path.exists(temp):
                os.unlink(temp)
        except OSError:
            pass


def download_state(
    rom: dict[str, Any],
    mapping: dict[str, Any],
    emulation_root: str,
    client,
    server: dict[str, Any] | None,
    state_id: int,
) -> dict[str, Any]:
    """Download one RomM state into the mapped emulator's state folder."""
    root = state_root_for(emulation_root, mapping)
    if not root:
        raise StateError("No save-state folder is known for this platform.")
    state = client.state(int(state_id)) or {}
    if int(state.get("rom_id") or 0) != int(rom["id"]):
        raise StateError("That save state belongs to a different game.")
    name = _active_state_name(rom, state)
    if not name:
        raise StateError("RomM did not give this state a usable file name.")
    response = client.open_state_content(int(state_id))
    try:
        content = response.read(MAX_UPLOAD + 1)
        content_type = (response.headers.get("Content-Type") or "").lower()
    finally:
        response.close()
    if len(content) > MAX_UPLOAD:
        raise StateError("That save state is larger than RomMArcade will download.")
    if not content:
        raise StateError("RomM returned an empty save state.")
    if "text/html" in content_type and content.lstrip().lower().startswith((b"<!doctype", b"<html")):
        raise StateError("RomM returned a web page instead of the save state.")
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, name)
    backup = _backup(target) if os.path.exists(target) else ""
    _write_atomic(target, content)
    try:
        stamp = datetime.fromisoformat(str(state.get("updated_at") or "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        os.utime(target, (stamp.timestamp(), stamp.timestamp()))
    except (TypeError, ValueError, OSError):
        pass
    return {
        "state_id": int(state_id),
        "file": name,
        "path": target,
        "bytes": len(content),
        "backup": backup,
        "root": root,
    }


def upload_state(
    rom: dict[str, Any],
    mapping: dict[str, Any],
    emulation_root: str,
    client,
    server: dict[str, Any] | None,
    name: str,
) -> dict[str, Any]:
    """Upload one state file from the mapped emulator's state folder."""
    root = state_root_for(emulation_root, mapping)
    if not root:
        raise StateError("No save-state folder is known for this platform.")
    clean = _safe_name(name)
    path = os.path.join(root, clean)
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(path)]) != os.path.abspath(root):
        raise StateError("That state is not in this game's state folder.")
    if not os.path.isfile(path):
        raise StateError("That save state is no longer on this machine.")
    if not matches_rom_stem(clean, rom_stem(rom)):
        raise StateError("That file does not belong to this game.")
    try:
        size = os.path.getsize(path)
    except OSError:
        raise StateError("That save state could not be read.") from None
    if size > MAX_UPLOAD:
        raise StateError("That save state is larger than RomMArcade will upload.")
    with open(path, "rb") as handle:
        content = handle.read()
    if not content:
        raise StateError("That save state is empty.")
    emulator = str(mapping.get("launcher") or "").strip() or None
    core = str(mapping.get("core") or "").strip() or None
    created = client.upload_state(
        int(rom["id"]),
        content,
        file_name=clean,
        emulator=emulator,
        core=core,
    )
    return {
        "file": clean,
        "path": path,
        "bytes": len(content),
        "state": _public(created or {}),
    }
