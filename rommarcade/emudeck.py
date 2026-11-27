"""EmuDeck integration: find its trees, map RomM platforms and launch games.

EmuDeck owns the emulators; RomMArcade only has to hand a ROM to the right
launcher.  Everything here is read-only discovery plus one detached exec, so a
user's EmuDeck setup is never rewritten.
"""

from __future__ import annotations

import glob
import json
import os
import re
import io
import shutil
import subprocess
import urllib.request
import zipfile
import sys
import threading
import time
from typing import Any

# RomM's fs_slug is normally the EmuDeck folder name.  These are the real
# disagreements between the two catalogues.
ALIASES: dict[str, list[str]] = {
    "ngc": ["gc"],
    "gc": ["gc"],
    "3ds": ["n3ds"],
    "n3ds": ["n3ds"],
    "dc": ["dreamcast"],
    "dreamcast": ["dreamcast"],
    "sega32": ["sega32x"],
    "sega32x": ["sega32x"],
    "sega-cd": ["segacd"],
    "megadrive": ["genesis"],
    "genesis": ["genesis"],
    "gg": ["gamegear"],
    "gamegear": ["gamegear"],
    "sms": ["mastersystem"],
    "mastersystem": ["mastersystem"],
    "ps1": ["psx"],
    "playstation": ["psx"],
    "psx": ["psx"],
    "famicom": ["nes"],
    "nes": ["nes"],
    "superfamicom": ["snes"],
    "snes": ["snes"],
    "turbografx16": ["pcengine"],
    "tg16": ["pcengine"],
    "pcengine": ["pcengine"],
    "pcenginecd": ["pcenginecd"],
    "turbografxcd": ["pcenginecd"],
    "nsw": ["switch"],
    "switch": ["switch"],
    "wiiu": ["wiiu"],
    "wii": ["wii"],
    "n64": ["n64"],
    "gb": ["gb"],
    "gbc": ["gbc"],
    "gba": ["gba"],
    "nds": ["nds"],
    "saturn": ["saturn"],
    "atari2600": ["atari2600"],
    "atari5200": ["atari5200"],
    "atari7800": ["atari7800"],
    "lynx": ["atarilynx", "lynx"],
    "xbox360": ["xbox360"],
    "xbox": ["xbox"],
    "psp": ["psp"],
    "ps3": ["ps3"],
    "ps4": ["ps4"],
    "arcade": ["arcade"],
    "mame": ["arcade", "mame"],
    "fbneo": ["arcade"],
    "naomi": ["arcade"],
    "model2": ["model2"],
    "virtualboy": ["virtualboy"],
    "wonderswan": ["wonderswan"],
    "wonderswancolor": ["wonderswancolor"],
    "ngp": ["ngp"],
    "ngpc": ["ngpc"],
    "vectrex": ["vectrex"],
    "c64": ["c64"],
    "amiga": ["amiga"],
    "amigacd32": ["amigacd32"],
    "dos": ["dos"],
    "scummvm": ["scummvm"],
    "easyrpg": ["easyrpg"],
    "pico8": ["pico8"],
    "ports": ["ports"],
}

# Systems whose EmuDeck setup includes a dedicated emulator launcher.
STANDALONE: dict[str, list[str]] = {
    "gc": ["dolphin-emu", "dolphin"],
    "wii": ["dolphin-emu", "dolphin"],
    "wiiu": ["cemu"],
    "psx": ["duckstation"],
    "ps2": ["pcsx2-qt", "pcsx2"],
    "psp": ["ppsspp"],
    "nds": ["melonds"],
    "n3ds": ["azahar", "lime3ds", "citra"],
    "switch": ["ryujinx", "sudachi", "yuzu"],
    "dreamcast": ["flycast"],
    "xbox": ["xemu-emu", "xemu"],
    "xbox360": ["xenia-emu", "xenia"],
    "ps3": ["rpcs3"],
    "ps4": ["shadps4"],
    "psvita": ["vita3k"],
    "model2": ["model-2-emulator", "supermodel"],
    "scummvm": ["scummvm"],
    "saturn": ["retroarch"],
    "arcade": ["retroarch"],
}

# Preferred libretro core per system, best first.  Only a core that exists on
# disk is ever chosen, so a partial RetroArch install degrades to the launcher
# without -L rather than to a black window.
CORES: dict[str, list[str]] = {
    "nes": ["mesen", "nestopia", "fceumm"],
    "fds": ["mesen", "nestopia"],
    "snes": ["snes9x", "bsnes", "mesen-s"],
    "gb": ["gambatte", "sameboy", "mgba"],
    "gbc": ["gambatte", "sameboy", "mgba"],
    "gba": ["mgba", "gambatte", "vbam"],
    "genesis": ["genesis_plus_gx", "picodrive"],
    "gamegear": ["genesis_plus_gx", "gearsystem"],
    "mastersystem": ["genesis_plus_gx", "gearsystem"],
    "sega32x": ["picodrive"],
    "segacd": ["genesis_plus_gx"],
    "n64": ["mupen64plus_next"],
    "atari2600": ["stella"],
    "atari5200": ["a5200", "atari800"],
    "atari7800": ["prosystem"],
    "atarilynx": ["mednafen_lynx", "handy"],
    "lynx": ["mednafen_lynx", "handy"],
    "pcengine": ["mednafen_pce_fast", "mednafen_pce"],
    "pcenginecd": ["mednafen_pce"],
    "neogeo": ["fbneo", "mame"],
    "arcade": ["fbneo", "mame", "mame2003_plus"],
    "mame": ["mame", "fbneo"],
    "saturn": ["kronos", "mednafen_saturn"],
    "psp": ["ppsspp"],
    "nds": ["melonds", "desmume"],
    "virtualboy": ["mednafen_vb"],
    "wonderswan": ["mednafen_wswan"],
    "wonderswancolor": ["mednafen_wswan"],
    "ngp": ["mednafen_ngp"],
    "ngpc": ["mednafen_ngp"],
    "vectrex": ["vecx"],
    "c64": ["vice_x64sc"],
    "amiga": ["puae"],
    "amigacd32": ["puae"],
    "dos": ["dosbox_pure"],
    "scummvm": ["scummvm"],
    "easyrpg": ["easyrpg"],
    "3do": ["opera"],
    "pcfx": ["mednafen_pcfx"],
    "atari800": ["atari800"],
    "chailove": ["chailove"],
}

EMULATOR_LABELS = {
    "retroarch": "RetroArch",
    "dolphin-emu": "Dolphin",
    "dolphin": "Dolphin",
    "cemu": "Cemu",
    "duckstation": "DuckStation",
    "pcsx2-qt": "PCSX2",
    "pcsx2": "PCSX2",
    "ppsspp": "PPSSPP",
    "melonds": "melonDS",
    "azahar": "Azahar",
    "lime3ds": "Lime3DS",
    "citra": "Citra",
    "ryujinx": "Ryujinx",
    "yuzu": "yuzu",
    "sudachi": "Sudachi",
    "flycast": "Flycast",
    "xenia-emu": "Xenia",
    "xenia": "Xenia",
    "xemu-emu": "xemu",
    "xemu": "xemu",
    "rpcs3": "RPCS3",
    "shadps4": "shadPS4",
    "vita3k": "Vita3K",
    "model-2-emulator": "Model 2 Emulator",
    "supermodel": "Supermodel",
    "scummvm": "ScummVM",
}

# Official project pages, used when EmuDeck cannot install the emulator for the
# user.  Keyed by the EmuDeck launcher id.
EMULATOR_WEBSITES = {
    "retroarch": "https://www.retroarch.com/",
    "dolphin-emu": "https://dolphin-emu.org/",
    "dolphin": "https://dolphin-emu.org/",
    "cemu": "https://cemu.info/",
    "duckstation": "https://www.duckstation.org/",
    "pcsx2-qt": "https://pcsx2.net/",
    "pcsx2": "https://pcsx2.net/",
    "ppsspp": "https://www.ppsspp.org/",
    "melonds": "https://melonds.kuribo64.net/",
    "azahar": "https://azahar-emu.org/",
    "lime3ds": "https://lime3ds.github.io/",
    "citra": "https://citra-emu.org/",
    "ryujinx": "https://ryujinx.app/",
    "yuzu": "https://yuzu-emu.org/",
    "sudachi": "https://github.com/sudachi-emu/sudachi",
    "flycast": "https://github.com/flyinghead/flycast",
    "xenia": "https://xenia.jp/",
    "xenia-emu": "https://xenia.jp/",
    "xemu-emu": "https://xemu.app/",
    "xemu": "https://xemu.app/",
    "rpcs3": "https://rpcs3.net/",
    "shadps4": "https://shadps4.net/",
    "vita3k": "https://vita3k.org/",
    "supermodel": "https://www.supermodel3.com/",
    "scummvm": "https://www.scummvm.org/",
}

# BIOS files an emulator cannot boot without when RomM does not track them as
# firmware.  Keyed by launcher id; the files live directly in the EmuDeck BIOS
# folder (xemu resolves them from <bios>/mcpx_1.0.bin and <bios>/Complex_4627*.bin).
EMULATOR_BIOS_SPEC: dict[str, list[dict[str, Any]]] = {
    "xemu-emu": [
        {"name": "mcpx_1.0.bin", "size": 512, "aliases": []},
        {
            "name": "Complex_4627v1.03.bin",
            "size": 1024 * 1024,
            "aliases": [
                "Complex_4627.bin", "Complex_4627v1.00.bin",
                "Complex_4627v1.02.bin", "Complex_4627v1.04.bin",
            ],
        },
    ],
}
EMULATOR_BIOS_SPEC["xemu"] = EMULATOR_BIOS_SPEC["xemu-emu"]
EMULATOR_BIOS: dict[str, list[str]] = {
    key: [entry["name"] for entry in entries] for key, entries in EMULATOR_BIOS_SPEC.items()
}


def _folder_files(folder: str) -> dict[str, str]:
    try:
        return {name.casefold(): name for name in os.listdir(folder)}
    except OSError:
        return {}


def bios_status(launcher_id: str, bios_root: str, platform_slug: str = "") -> list[dict[str, Any]]:
    """Where each required BIOS file is, whether its size is right, and how to fix it."""
    specs = EMULATOR_BIOS_SPEC.get((launcher_id or "").casefold(), [])
    if not specs:
        return []
    root_files = _folder_files(bios_root) if bios_root else {}
    platform_folder = os.path.join(bios_root, platform_slug) if bios_root and platform_slug else ""
    platform_files = _folder_files(platform_folder) if platform_folder else {}
    out: list[dict[str, Any]] = []
    for spec in specs:
        name = spec["name"]
        size = int(spec["size"])
        aliases = list(spec.get("aliases") or [])
        entry: dict[str, Any] = {
            "name": name,
            "size": size,
            "path": os.path.join(bios_root, name) if bios_root else "",
            "found": "",
            "actual_size": 0,
            "status": "missing",
            "can_repair": False,
        }
        actual_name = root_files.get(name.casefold())
        if actual_name:
            path = os.path.join(bios_root, actual_name)
            actual = os.path.getsize(path)
            entry.update(found=path, actual_size=actual)
            entry["status"] = "present" if actual == size else "invalid"
            out.append(entry)
            continue
        for alias in aliases:
            actual_name = root_files.get(alias.casefold())
            if actual_name:
                path = os.path.join(bios_root, actual_name)
                actual = os.path.getsize(path)
                entry.update(found=path, actual_size=actual)
                entry["status"] = "alias" if actual == size else "invalid"
                entry["can_repair"] = actual == size
                break
        if entry["found"]:
            out.append(entry)
            continue
        for candidate in [name] + aliases:
            actual_name = platform_files.get(candidate.casefold())
            if actual_name:
                path = os.path.join(platform_folder, actual_name)
                actual = os.path.getsize(path)
                entry.update(found=path, actual_size=actual)
                entry["status"] = "misplaced" if actual == size else "invalid"
                entry["can_repair"] = actual == size
                break
        out.append(entry)
    return out


def repair_emulator_bios(launcher_id: str, bios_root: str, platform_slug: str = "") -> dict[str, Any]:
    """Copy a valid file found under the platform folder or an alias to its expected path."""
    result: dict[str, Any] = {"copied": [], "failed": []}
    if not bios_root:
        return result
    for entry in bios_status(launcher_id, bios_root, platform_slug):
        if entry["status"] == "present":
            continue
        if entry["can_repair"] and entry["found"]:
            try:
                os.makedirs(bios_root, exist_ok=True)
                shutil.copy2(entry["found"], entry["path"])
                result["copied"].append({"name": entry["name"], "from": entry["found"]})
            except OSError as exc:
                result["failed"].append({"name": entry["name"], "reason": str(exc)})
        elif entry["status"] == "invalid":
            result["failed"].append({
                "name": entry["name"],
                "reason": f"{entry['actual_size']} bytes; expected {entry['size']}",
            })
    result["status"] = bios_status(launcher_id, bios_root, platform_slug)
    return result


def missing_emulator_bios(launcher_id: str, bios_root: str, platform_slug: str = "") -> list[str]:
    return [
        entry["name"] for entry in bios_status(launcher_id, bios_root, platform_slug)
        if entry["status"] != "present"
    ]


# System files an emulator needs beyond BIOS.  Keyed by launcher id; paths are
# relative to the EmuDeck storage folder.  A missing hard-drive image is a
# launch blocker; the EEPROM is reported but xemu can create one on first boot.
EMULATOR_STORAGE: dict[str, list[dict[str, Any]]] = {
    "xemu-emu": [
        {
            "kind": "hdd",
            "label": "Xbox hard drive",
            "dir": "xemu",
            "file": "xbox_hdd.qcow2",
            "size": 8 * 1024 ** 3,
            "download": "https://github.com/mborgerson/xemu-hdd-image/releases/latest/download/xbox_hdd.qcow2.zip",
            "member": "xbox_hdd.qcow2",
        },
        {
            "kind": "eeprom",
            "label": "Xbox EEPROM",
            "dir": "xemu",
            "file": "eeprom.bin",
            "size": 256,
        },
    ],
}
EMULATOR_STORAGE["xemu"] = EMULATOR_STORAGE["xemu-emu"]


def _qcow2_virtual_size(path: str) -> int:
    """The virtual disk size in a qcow2 header (0 when it is not qcow2)."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
    except OSError:
        return 0
    if len(header) < 32 or header[:4] != b"QFI\xfb":
        return 0
    return int.from_bytes(header[24:32], "big")


def storage_status(launcher_id: str, storage_root: str) -> list[dict[str, Any]]:
    specs = EMULATOR_STORAGE.get((launcher_id or "").casefold(), [])
    out: list[dict[str, Any]] = []
    for spec in specs:
        folder = os.path.join(storage_root, spec["dir"]) if storage_root else ""
        path = os.path.join(folder, spec["file"]) if folder else ""
        entry: dict[str, Any] = {
            "kind": spec["kind"],
            "label": spec["label"],
            "file": spec["file"],
            "path": path,
            "size": int(spec["size"]),
            "actual_size": 0,
            "virtual_size": 0,
            "present": False,
            "status": "missing",
            "download": spec.get("download") or "",
        }
        if path and os.path.isfile(path):
            actual = os.path.getsize(path)
            virtual = _qcow2_virtual_size(path) if spec["kind"] == "hdd" else 0
            entry.update(actual_size=actual, virtual_size=virtual)
            if spec["kind"] == "hdd":
                entry["present"] = actual > 0 and (virtual >= spec["size"] or actual >= spec["size"])
            else:
                entry["present"] = actual == spec["size"]
            entry["status"] = "present" if entry["present"] else "invalid"
        out.append(entry)
    return out


def download_emulator_storage(launcher_id: str, storage_root: str, kind: str = "hdd") -> dict[str, Any]:
    """Fetch a system image the emulator ships separately (xemu's HDD)."""
    specs = EMULATOR_STORAGE.get((launcher_id or "").casefold(), [])
    spec = next((item for item in specs if item["kind"] == kind), None)
    if spec is None:
        raise EmuDeckError(f"{launcher_id or 'This emulator'} has no {kind} image to download.")
    url = spec.get("download") or ""
    if not url:
        raise EmuDeckError(f"The {spec['label']} cannot be downloaded automatically.")
    if not storage_root:
        raise EmuDeckError("No EmuDeck storage folder was found.")
    folder = os.path.join(storage_root, spec["dir"])
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, spec["file"])
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            payload = response.read()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            data = archive.read(spec.get("member") or spec["file"])
    except Exception as exc:
        raise EmuDeckError(f"Could not download the {spec['label']}: {exc}") from exc
    if spec["kind"] == "hdd" and data[:4] != b"QFI\xfb":
        raise EmuDeckError("The downloaded hard-drive image is not a qcow2 file.")
    if os.path.exists(target):
        shutil.copy2(target, f"{target}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    temporary = target + ".part"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, target)
    return {"path": target, "bytes": len(data), "status": storage_status(launcher_id, storage_root)}


_LAUNCHER_CACHE: dict[str, Any] = {"fingerprint": None, "data": None, "lock": threading.Lock()}
_FLATPAK_CACHE: dict[str, Any] = {"at": 0.0, "apps": set()}
# Launchers whose script finds a sidecar binary at runtime; an empty backend
# string is not proof they are installed (ryujinx is the usual corpse).
SIDECAR_EMULATORS = {
    "ryujinx": "ryujinx",
    "yuzu": "yuzu",
    "sudachi": "sudachi",
    "citra": "citra",
    "lime3ds": "lime3ds",
    "xenia": "xenia",
    "xenia-emu": "xenia",
}


class EmuDeckError(RuntimeError):
    pass


def emudeck_settings_path() -> str:
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(root, "EmuDeck", "settings.json")
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "EmuDeck", "settings.json"
        )
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "EmuDeck",
        "settings.json",
    )


def _read_settings() -> dict[str, Any]:
    path = emudeck_settings_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_roots(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the active game tree, honouring explicit config overrides.

    Two kinds of tree can own the games: an EmuDeck ``Emulation`` folder, or a
    plain library the user chose when EmuDeck is not installed on this machine.
    An explicitly configured library wins over EmuDeck auto-detection: it is a
    decision the user made, while detection is only a guess.
    """
    config = config or {}
    emu_cfg = config.get("emudeck") or {}
    library = library_roots(config)
    if library["root"]:
        return {
            "mode": "library",
            "detected": False,
            "storage_path": os.path.dirname(library["root"]),
            "root": "",
            "roms_root": library["roms_root"],
            "bios_root": library["bios_root"],
            "launchers_root": "",
            "settings": _read_settings(),
            "library": library,
        }
    settings = _read_settings()
    manual = str(emu_cfg.get("root") or "").strip()
    settings_storage = str(settings.get("storagePath") or "").strip()
    storage = manual or settings_storage
    candidates: list[str] = []
    if manual:
        if os.path.basename(os.path.abspath(manual)) == "Emulation":
            # An explicit Emulation folder is a user decision; keep it.
            candidates.append(manual)
        else:
            # A storage base, not a tree: EmuDeck puts the tree in
            # <storagePath>/Emulation, and that is what its launchers use.
            if settings_storage:
                candidates.append(os.path.join(settings_storage, "Emulation"))
            candidates.append(os.path.join(manual, "Emulation"))
            candidates.append(manual)
    elif settings_storage:
        candidates.append(os.path.join(settings_storage, "Emulation"))
        candidates.append(settings_storage)
    home = os.path.expanduser("~")
    candidates.extend(
        [
            os.path.join(home, "Emulation"),
            os.path.join(home, ".local", "share", "Emulation"),
        ]
    )
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or home
        local = os.environ.get("LOCALAPPDATA") or appdata
        candidates.extend(
            [
                os.path.join(appdata, "EmuDeck", "Emulation"),
                os.path.join(local, "EmuDeck", "Emulation"),
                os.path.join(home, "AppData", "Roaming", "EmuDeck", "Emulation"),
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                os.path.join(home, "Library", "Application Support", "EmuDeck", "Emulation"),
                os.path.join(home, "Library", "Application Support", "Emulation"),
            ]
        )
    emulation_root = ""
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            if os.path.basename(os.path.abspath(candidate)) == "Emulation":
                emulation_root = os.path.abspath(candidate)
            elif os.path.isdir(os.path.join(candidate, "roms")):
                emulation_root = os.path.abspath(candidate)
            if emulation_root:
                break
    if not emulation_root:
        return {
            "mode": "",
            "detected": False,
            "storage_path": storage,
            "root": "",
            "roms_root": "",
            "bios_root": "",
            "launchers_root": "",
            "settings": settings,
            "library": library,
        }
    roms = str(emu_cfg.get("roms_root") or os.path.join(emulation_root, "roms"))
    bios = str(emu_cfg.get("bios_root") or os.path.join(emulation_root, "bios"))
    launchers = str(
        emu_cfg.get("launchers_root") or os.path.join(emulation_root, "tools", "launchers")
    )
    return {
        "mode": "emudeck",
        "detected": True,
        "storage_path": storage or os.path.dirname(emulation_root),
        "root": emulation_root,
        "roms_root": roms,
        "bios_root": bios,
        "launchers_root": launchers,
        "settings": settings,
        "library": library,
    }


def _library_root_value(raw: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(raw or "").strip())))


def library_roots(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the plain ``library`` config section into ROM and BIOS roots.

    ``mode: romm`` is the server's own layout (``<root>/roms`` plus
    ``<root>/bios``); ``mode: custom`` uses the folder itself for ROMs and a
    sibling ``bios`` folder unless the user pointed at a ``roms`` folder.
    """
    library = (config or {}).get("library") or {}
    mode = str(library.get("mode") or "").strip().lower()
    raw = str(library.get("root") or "").strip()
    root = _library_root_value(raw) if raw else ""
    roms = ""
    bios = ""
    if root:
        if mode == "romm":
            roms = os.path.join(root, "roms")
            bios = _library_root_value(library.get("bios_root")) if library.get("bios_root") else os.path.join(root, "bios")
        else:
            roms = root
            if library.get("bios_root"):
                bios = _library_root_value(library.get("bios_root"))
            elif os.path.basename(root) == "roms":
                bios = os.path.join(os.path.dirname(root), "bios")
            else:
                bios = os.path.join(root, "bios")
    return {
        "mode": mode if root else "",
        "root": root,
        "roms_root": roms,
        "bios_root": bios,
        "exists": bool(roms and os.path.isdir(roms)),
    }


def suggest_library_root(mode: str) -> str:
    home = os.path.expanduser("~")
    return os.path.join(home, "RomM" if str(mode or "").lower() == "romm" else "ROMs")


def plan_library(mode: str, root: str) -> dict[str, Any]:
    """Validate a requested layout and return the folders it would use."""
    mode = str(mode or "").strip().lower()
    if mode not in ("romm", "custom"):
        raise EmuDeckError("Choose the RomM layout or a folder of your own.")
    raw = str(root or "").strip()
    if not raw:
        raise EmuDeckError("Choose a folder for the game library.")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise EmuDeckError("Use an absolute folder path, for example " + suggest_library_root(mode))
    base = os.path.normpath(expanded)
    if base == os.path.sep:
        raise EmuDeckError("A library cannot be the filesystem root.")
    if os.path.exists(base) and not os.path.isdir(base):
        raise EmuDeckError(f"{base} is a file, not a folder.")
    if mode == "romm":
        roms = os.path.join(base, "roms")
        bios = os.path.join(base, "bios")
    else:
        roms = base
        bios = os.path.join(os.path.dirname(base), "bios") if os.path.basename(base) == "roms" else os.path.join(base, "bios")
    return {"mode": mode, "root": base, "roms_root": roms, "bios_root": bios}


def create_library(plan: dict[str, Any], platforms: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a library tree, one folder per RomM platform with games.

    Existing folders win over new ones, including alias-named ones such as
    ``psx`` for a ``ps1`` platform, so pointing at a collection the user
    already has never duplicates it.
    """
    roms = str(plan.get("roms_root") or "")
    bios = str(plan.get("bios_root") or "")
    if not roms:
        raise EmuDeckError("The library plan has no ROM folder.")
    os.makedirs(roms, exist_ok=True)
    if bios:
        os.makedirs(bios, exist_ok=True)
    existing = sorted(
        name for name in os.listdir(roms) if os.path.isdir(os.path.join(roms, name))
    )
    created: list[str] = []
    mapped: list[dict[str, Any]] = []
    for platform in platforms:
        if platform.get("is_game_platform") is False:
            continue
        games = int(platform.get("game_count") or platform.get("rom_count") or 0)
        if games <= 0:
            continue
        slug = str(platform.get("fs_slug") or platform.get("slug") or "").strip()
        if not slug:
            continue
        found = find_platform_dir(slug, roms, existing)
        if found:
            mapped.append({"platform": slug, "dir": found, "created": False})
            continue
        folder = slug if re.fullmatch(r"[A-Za-z0-9._-]+", slug) else sanitize_name(slug)
        target = os.path.join(roms, folder)
        if not os.path.isdir(target):
            os.makedirs(target, exist_ok=True)
            created.append(folder)
        existing.append(folder)
        mapped.append({"platform": slug, "dir": folder, "created": True})
    return {"created": created, "platforms": mapped}


def _resolve_script_token(token: str, emulation_root: str) -> str:
    """Resolve the EmuDeck variables a launcher script puts in front of its binary."""
    text = str(token or "").strip().strip("\"'")
    home = os.path.expanduser("~")
    variables = {
        "${HOME}": home,
        "$HOME": home,
        "${romsPath}": os.path.join(emulation_root, "roms"),
        "$romsPath": os.path.join(emulation_root, "roms"),
        "${toolsPath}": os.path.join(emulation_root, "tools"),
        "$toolsPath": os.path.join(emulation_root, "tools"),
        "${biosPath}": os.path.join(emulation_root, "bios"),
        "$biosPath": os.path.join(emulation_root, "bios"),
        "${savesPath}": os.path.join(emulation_root, "saves"),
        "$savesPath": os.path.join(emulation_root, "saves"),
    }
    if not emulation_root:
        for key in [name for name in variables if "HOME" not in name]:
            variables[key] = ""
    for variable, value in variables.items():
        if value:
            text = text.replace(variable, value)
    if not text or "$" in text or not os.path.isabs(text):
        return ""
    return os.path.normpath(text)


def _script_backend(path: str, emulation_root: str = "") -> str:
    """What a launcher needs: a flatpak id, an AppImage, or a Proton/Wine exe.

    EmuDeck's Xenia launcher is a Proton wrapper around
    ``roms/xbox360/xenia_canary.exe``; there is no AppImage or flatpak to find,
    so readiness has to come from the executable the script names.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(20000)
    except OSError:
        return ""
    match = re.search(r"flatpak\s+run\s+([A-Za-z0-9_.-]+)", text)
    if match:
        return "flatpak:" + match.group(1)
    missing = ""
    if re.search(r"proton-launch|\bwine\b", text, re.IGNORECASE):
        for token in re.findall(r"""[^\s"']+\.exe\b""", text, re.IGNORECASE):
            resolved = _resolve_script_token(token, emulation_root)
            if not resolved:
                continue
            if os.path.exists(resolved):
                return "proton:" + resolved
            missing = missing or "proton:" + resolved
        if missing:
            return missing
    for line in text.splitlines():
        if "AppImage" in line:
            for token in re.findall(r"""[^\s"']+\.AppImage""", line):
                resolved = _resolve_script_token(token, emulation_root)
                if not resolved:
                    continue
                if os.path.exists(resolved):
                    return "appimage:" + resolved
                missing = missing or "appimage:" + resolved
    return missing


def _backend_ready(backend: str, stem: str = "") -> bool:
    if not backend:
        return _launcher_binary_present(stem)
    kind, _, value = backend.partition(":")
    if kind == "proton":
        return os.path.exists(value)
    if kind == "appimage":
        return os.path.exists(value)
    if kind == "flatpak":
        flatpak = shutil.which("flatpak")
        if not flatpak:
            return True  # discovery is running somewhere without flatpak
        return value in _flatpak_apps()
    return True


def _flatpak_apps() -> set[str]:
    now = time.time()
    if now - float(_FLATPAK_CACHE["at"]) < 30 and _FLATPAK_CACHE["apps"]:
        return set(_FLATPAK_CACHE["apps"])
    flatpak = shutil.which("flatpak")
    apps: set[str] = set()
    if flatpak:
        try:
            out = subprocess.run(
                [flatpak, "list", "--app", "--columns=application"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            apps = set(out.stdout.split())
        except (OSError, subprocess.SubprocessError):
            apps = set()
    _FLATPAK_CACHE["at"] = now
    _FLATPAK_CACHE["apps"] = apps
    return set(apps)


def _launcher_binary_present(stem: str) -> bool:
    wanted = SIDECAR_EMULATORS.get(stem)
    if not wanted:
        return True
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, "Applications"),
        os.path.join(home, ".local", "share", "Applications"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for pattern in ("*.AppImage", "*/*.AppImage", "publish/*", "*/*/*.AppImage"):
            for path in glob.glob(os.path.join(root, pattern)):
                if wanted in os.path.basename(path).lower():
                    return True
    return any(wanted in app.lower() for app in _flatpak_apps())


def discover_launchers(launchers_root: str, available_cores: set[str], emulation_root: str = "") -> list[dict[str, Any]]:
    if not os.path.isdir(launchers_root):
        return []
    patterns = ["*.sh"]
    if sys.platform == "win32":
        patterns.extend(["*.bat", "*.cmd"])
    elif sys.platform == "darwin":
        patterns.extend(["*.command"])
    seen: dict[str, dict[str, Any]] = {}
    paths = []
    for pattern in patterns:
        paths.extend(sorted(glob.glob(os.path.join(launchers_root, pattern))))
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        real = os.path.realpath(path)
        if real in {entry["real_path"] for entry in seen.values()}:
            continue
        backend = _script_backend(real, emulation_root)
        seen[stem] = {
            "id": stem,
            "name": EMULATOR_LABELS.get(stem, stem.replace("-", " ").title()),
            "path": path,
            "real_path": real,
            "backend": backend,
            "ready": _backend_ready(backend, stem),
        }
    return list(seen.values())


def mark_superseded(launchers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag a broken launcher variant whose emulator has a working one.

    EmuDeck can leave both ``xenia.sh`` (Proton, working) and ``xenia-emu.sh``
    (an AppImage that is gone).  Both are named Xenia, so counting the broken
    one as a missing emulator is a false warning and a duplicate in a picker.
    """
    ready_names = {
        str(launcher.get("name") or "").strip().lower()
        for launcher in launchers
        if launcher.get("ready")
    }
    for launcher in launchers:
        name = str(launcher.get("name") or "").strip().lower()
        launcher["superseded"] = bool(not launcher.get("ready") and name and name in ready_names)
    return launchers


def discover_cores() -> list[str]:
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, ".var/app/org.libretro.RetroArch/config/retroarch/cores"),
        os.path.join(home, ".config/retroarch/cores"),
        os.path.join(home, "Emulation/tools/retroarch/cores"),
    ]
    extensions = [".so"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or home
        roots.extend([
            os.path.join(appdata, "RetroArch", "cores"),
            os.path.join(home, "AppData", "Roaming", "RetroArch", "cores"),
        ])
        extensions = [".dll"]
    elif sys.platform == "darwin":
        roots.extend([
            os.path.join(home, "Library", "Application Support", "RetroArch", "cores"),
        ])
        extensions = [".dylib"]
    found: set[str] = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for extension in extensions:
            for path in glob.glob(os.path.join(root, "*" + extension)):
                name = os.path.basename(path)
                stem = name[: -len(extension)]
                if stem.endswith("_libretro"):
                    stem = stem[: -len("_libretro")]
                found.add(stem)
    return sorted(found)


def _normalize_dir(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_platform_dir(fs_slug: str, roms_root: str, existing: list[str] | None = None) -> str | None:
    if not roms_root or not os.path.isdir(roms_root):
        return None
    existing = existing if existing is not None else sorted(
        d for d in os.listdir(roms_root) if os.path.isdir(os.path.join(roms_root, d))
    )
    lower = {d.lower(): d for d in existing}
    key = (fs_slug or "").lower()
    if key in lower:
        return lower[key]
    for alias in ALIASES.get(key, []):
        if alias.lower() in lower:
            return lower[alias.lower()]
    target = _normalize_dir(key)
    for directory in existing:
        if _normalize_dir(directory) == target:
            return directory
    return None


def choose_launcher(fs_slug: str, launchers: list[dict[str, Any]], cores: set[str] | None = None) -> dict[str, Any] | None:
    ready = {entry["id"]: entry for entry in launchers if entry.get("ready", True)}
    available = {entry["id"]: entry for entry in launchers}
    candidates = list(STANDALONE.get(fs_slug, []))
    for candidate in candidates:
        if candidate in ready:
            return ready[candidate]
    if "retroarch" in ready:
        if cores is None or choose_core(fs_slug, cores):
            return ready["retroarch"]
        if fs_slug not in STANDALONE:
            return ready["retroarch"]
    # Keep the intended emulator in the mapping even when its backend is
    # missing, so the game page can offer to install it instead of pretending
    # the platform was never mapped.
    for candidate in candidates:
        if candidate in available:
            return available[candidate]
    if candidates:
        # No launcher script at all: keep a stub so the mapping names the
        # emulator (xemu, RPCS3, …) and the game page can offer to install it.
        first = candidates[0]
        return {
            "id": first,
            "name": EMULATOR_LABELS.get(first, first.replace("-", " ").title()),
            "path": "",
            "real_path": "",
            "backend": "",
            "ready": False,
            "stub": True,
        }
    if "retroarch" in available:
        return available["retroarch"]
    return None


def choose_core(fs_slug: str, cores: set[str]) -> str | None:
    for candidate in CORES.get(fs_slug, []):
        if candidate in cores:
            return candidate
    return None


def platform_choices(fs_slug: str, launchers: list[dict[str, Any]], cores: list[str]) -> dict[str, Any]:
    """The launchers and RetroArch cores that can actually run one system.

    The mapping table used to offer every launcher and every installed core for
    every platform, which invites DuckStation on a Mega Drive.  This is the same
    knowledge ``choose_launcher`` uses, exposed for the picker.
    """
    key = (fs_slug or "").lower()
    available = [launcher for launcher in launchers if not launcher.get("superseded")]
    by_id = {launcher["id"]: launcher for launcher in available}
    wanted: list[str] = []
    for candidate in STANDALONE.get(key, []):
        if candidate in by_id and candidate not in wanted:
            wanted.append(candidate)
    installed = set(cores)
    core_names = [core for core in CORES.get(key, []) if core in installed]
    if key not in CORES and key not in STANDALONE:
        # An unknown system keeps every installed core: the list is a fallback,
        # not a claim about what it can run.
        core_names = sorted(installed)
    if (
        "retroarch" in by_id
        and "retroarch" not in wanted
        and (core_names or key not in STANDALONE)
    ):
        wanted.append("retroarch")
    if not wanted and key not in STANDALONE:
        # An unknown system has no rule to narrow by; a known one with nothing
        # installed stays empty on purpose, so no invalid emulator is offered.
        wanted = [launcher["id"] for launcher in available]
    return {
        "launchers": wanted,
        "cores": core_names,
        "core_launcher": "retroarch",
    }


def platform_mapping(
    platform: dict[str, Any],
    roots: dict[str, Any],
    launchers: list[dict[str, Any]],
    cores: list[str],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one RomM platform to an EmuDeck folder, launcher and core."""
    overrides = overrides or {}
    fs_slug = str(platform.get("fs_slug") or platform.get("slug") or "")
    override = overrides.get(fs_slug) or overrides.get(str(platform.get("slug") or "")) or {}
    roms_root = roots.get("roms_root") or ""
    directory = override.get("dir") or find_platform_dir(fs_slug, roms_root)
    path = os.path.join(roms_root, directory) if directory and roms_root else ""
    launcher = None
    launcher_id = override.get("launcher")
    if launcher_id:
        launcher = next((l for l in launchers if l["id"] == launcher_id), None)
    if launcher is None:
        launcher = choose_launcher(fs_slug, launchers, set(cores))
    elif not launcher.get("ready", False):
        # A stored preference for a variant EmuDeck left incomplete (the
        # AppImage Xenia, say) must not hide a working sibling launcher.
        fallback = choose_launcher(fs_slug, launchers, set(cores))
        if fallback is not None and fallback.get("ready", False):
            launcher = fallback
    core = override.get("core")
    if core is None:
        core = choose_core(fs_slug, set(cores)) if (launcher or {}).get("id") == "retroarch" else None
    launcher_key = (launcher or {}).get("id") or ""
    bios_root = roots.get("bios_root") or ""
    bios_state = bios_status(launcher_key, bios_root, fs_slug)
    storage_root = os.path.join(roots.get("root") or "", "storage") if roots.get("root") else ""
    storage_state = storage_status(launcher_key, storage_root)
    return {
        "platform": fs_slug,
        "slug": platform.get("slug"),
        "name": platform.get("display_name") or platform.get("name") or fs_slug,
        "dir": directory or "",
        "path": path,
        "exists": bool(path and os.path.isdir(path)),
        "launcher": (launcher or {}).get("id") or "",
        "launcher_name": (launcher or {}).get("name") or "",
        "launcher_path": (launcher or {}).get("path") or "",
        "launcher_ready": bool((launcher or {}).get("ready", False)),
        "launcher_url": EMULATOR_WEBSITES.get((launcher or {}).get("id") or "", ""),
        "bios_required": [entry["name"] for entry in bios_state],
        "bios_missing": [entry["name"] for entry in bios_state if entry["status"] != "present"],
        "bios_status": bios_state,
        "bios_folder": bios_root,
        "storage_status": storage_state,
        "storage_missing": [
            entry["label"] for entry in storage_state
            if entry["kind"] == "hdd" and entry["status"] != "present"
        ],
        "storage_folder": storage_root,
        "core": core or "",
        "source": "override" if override else ("alias" if directory and directory.lower() != fs_slug.lower() else "catalogue"),
    }


def rom_file(rom: dict[str, Any]) -> dict[str, Any] | None:
    files = rom.get("files") or []
    top = [f for f in files if f.get("is_top_level")] or files
    if not top:
        return None
    return top[0]


def is_multi_file(rom: dict[str, Any]) -> bool:
    """True when the game is a tree, not one file.

    RomM sets ``has_multiple_files`` for folders, but a single archive that
    contains a whole game (an Xbox rip, for example) reports ``False`` while
    its ``files`` list holds every member.  Either shape must install into a
    directory and be extracted.
    """
    return bool(rom.get("has_multiple_files")) or len(rom.get("files") or []) > 1


def rom_target_name(rom: dict[str, Any]) -> str:
    if is_multi_file(rom):
        stem = rom.get("fs_name_no_ext") or rom.get("fs_name") or rom.get("name") or f"rom-{rom.get('id')}"
        return sanitize_name(stem)
    file_info = rom_file(rom) or {}
    name = file_info.get("file_name") or rom.get("fs_name") or rom.get("file_name")
    if not name:
        base = rom.get("name") or f"rom-{rom.get('id')}"
        extension = rom.get("fs_extension") or ""
        return sanitize_name(base + (("." + extension) if extension else ""))
    return sanitize_name(name)


def install_path(rom: dict[str, Any], mapping: dict[str, Any]) -> str:
    directory = mapping.get("path") or ""
    if not directory:
        raise EmuDeckError(
            f"No game folder is set for {mapping.get('name') or 'this platform'}. "
            "Choose where games should be stored in Settings, Emulators."
        )
    return os.path.join(directory, rom_target_name(rom))


def sanitize_name(name: str) -> str:
    cleaned = str(name).replace("\x00", "").replace("/", "-").replace("\\", "-").strip()
    cleaned = cleaned.lstrip(".")
    return cleaned or "rom"


def bios_target(firmware: dict[str, Any], bios_root: str) -> str:
    relative = str(firmware.get("file_path") or "").replace("\\", "/").lstrip("/")
    if relative.startswith("bios/"):
        relative = relative[len("bios/") :]
    elif relative == "bios":
        relative = ""
    parts = [p for p in relative.split("/") if p and p not in (".", "..")]
    file_name = sanitize_name(firmware.get("file_name") or "firmware.bin")
    return os.path.join(bios_root, *parts, file_name)


def save_roots(emulation_root: str, platform_dir: str) -> list[str]:
    """EmuDeck's per-game save and save-state folders for one platform."""
    directory = sanitize_name(platform_dir or "")
    if not emulation_root or not directory:
        return []
    return [
        os.path.join(emulation_root, "saves", directory),
        os.path.join(emulation_root, "states", directory),
    ]


def _save_matches(name: str, stem: str) -> bool:
    lowered = name.lower()
    wanted = stem.lower()
    return (
        lowered == wanted
        or lowered.startswith(wanted + ".")
        or lowered.startswith(wanted + " ")
    )


def matches_rom_stem(name: str, stem: str) -> bool:
    """Whether a save name belongs to one ROM, by EmuDeck's naming rule."""
    return bool(name and stem) and _save_matches(name, stem)


def remove_game_saves(emulation_root: str, platform_dir: str, rom_stem: str) -> list[str]:
    """Remove save files and states whose names belong to one installed ROM.

    Only immediate children of EmuDeck's saves/states folders are touched, and
    only when the name is the ROM stem or the stem plus a suffix.  Memory-card
    images shared by a whole system are deliberately left alone.
    """
    removed: list[str] = []
    stem = sanitize_name(rom_stem or "")
    if not stem:
        return removed
    for root in save_roots(emulation_root, platform_dir):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if not _save_matches(name, stem):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.islink(path) or os.path.isfile(path):
                    os.unlink(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    continue
            except OSError:
                continue
            removed.append(os.path.relpath(path, emulation_root))
    return removed


def xemu_disc_path(file_path: str) -> str:
    """xemu only accepts an XISO image; find one for a loose-file game folder."""
    if os.path.isfile(file_path) and file_path.casefold().endswith((".iso", ".xiso")):
        return file_path
    if os.path.isdir(file_path):
        folder = file_path.rstrip("/\\")
        for name in ("rommarcade.iso", "game.iso", os.path.basename(folder) + ".iso"):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
        try:
            for entry in sorted(os.listdir(folder)):
                if entry.casefold().endswith((".iso", ".xiso")):
                    return os.path.join(folder, entry)
        except OSError:
            pass
    sibling = file_path.rstrip("/\\") + ".iso"
    return sibling if os.path.isfile(sibling) else ""


def build_command(mapping: dict[str, Any], file_path: str) -> list[str]:
    launcher = mapping.get("launcher_path") or ""
    if not launcher or not os.path.exists(launcher):
        raise EmuDeckError(
            f"No EmuDeck launcher is installed for {mapping.get('name') or 'this platform'}."
        )
    if sys.platform == "win32" and launcher.lower().endswith((".bat", ".cmd")):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", launcher]
    else:
        command = [launcher]
    if mapping.get("launcher") in ("xemu", "xemu-emu"):
        disc = xemu_disc_path(file_path)
        if not disc:
            raise EmuDeckError(
                "xemu needs an XISO disc image, but this game is installed as loose files. "
                "Repack it with xdvdfs, or install it from an .iso."
            )
        return command + ["-cdrom", disc]
    if mapping.get("launcher") == "retroarch" and mapping.get("core"):
        command += ["-L", mapping["core"]]
    command.append(file_path)
    return command


def spawn_process(command: list[str], log_file: str, env: dict[str, str] | None = None):
    """Start an emulator detached from the app, returning the process handle."""
    user_env = dict(os.environ if env is None else env)
    user_env.setdefault("HOME", os.path.expanduser("~"))
    if sys.platform.startswith("linux"):
        runtime = user_env.get("XDG_RUNTIME_DIR") or (
            f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else ""
        )
        if runtime:
            user_env["XDG_RUNTIME_DIR"] = runtime
            if not user_env.get("WAYLAND_DISPLAY") and os.path.isdir(runtime):
                for socket in sorted(glob.glob(os.path.join(runtime, "wayland-*"))):
                    if not socket.endswith(".lock"):
                        user_env["WAYLAND_DISPLAY"] = os.path.basename(socket)
                        break
        user_env.setdefault("DISPLAY", ":0")
        user_env["PATH"] = user_env.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    handle = open(log_file, "ab", buffering=0)
    try:
        handle.write(
            f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} rommarcade: {' '.join(command)}\n".encode()
        )
        kwargs: dict[str, Any] = {
            "stdout": handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": user_env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
    finally:
        handle.close()
    return process


def spawn(command: list[str], log_file: str, env: dict[str, str] | None = None) -> int:
    """Start an emulator detached from the app, returning its pid."""
    return spawn_process(command, log_file, env).pid


def detect(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full EmuDeck/library snapshot, cheap enough to cache for a few seconds."""
    config = config or {}
    emu_cfg = config.get("emudeck") or {}
    fingerprint = json.dumps(
        {
            "settings_mtime": _mtime(emudeck_settings_path()),
            "root": emu_cfg.get("root"),
            "roms": emu_cfg.get("roms_root"),
            "launchers": emu_cfg.get("launchers_root"),
            "library": config.get("library"),
        },
        sort_keys=True,
    )
    with _LAUNCHER_CACHE["lock"]:
        if _LAUNCHER_CACHE["fingerprint"] == fingerprint and _LAUNCHER_CACHE["data"] is not None:
            return _LAUNCHER_CACHE["data"]
    roots = resolve_roots(config)
    cores = discover_cores()
    launchers = discover_launchers(roots["launchers_root"], set(cores), roots["root"]) if roots["mode"] == "emudeck" else []
    launchers = mark_superseded(launchers)
    rom_dirs = []
    if os.path.isdir(roots["roms_root"]):
        try:
            rom_dirs = sorted(
                name
                for name in os.listdir(roots["roms_root"])
                if os.path.isdir(os.path.join(roots["roms_root"], name))
            )
        except OSError:
            rom_dirs = []
    library = roots.get("library") or library_roots(config)
    data = {
        "mode": roots["mode"],
        "detected": roots["mode"] == "emudeck",
        "usable": bool(roots["mode"]) and os.path.isdir(roots["roms_root"]),
        "storage_path": roots["storage_path"],
        "root": roots["root"],
        "roms_root": roots["roms_root"],
        "bios_root": roots["bios_root"],
        "launchers_root": roots["launchers_root"],
        "library": {**library, "configured": bool(library.get("root"))},
        "settings_path": emudeck_settings_path(),
        "rom_dirs": rom_dirs,
        "launchers": launchers,
        "cores": cores,
        "settings": {
            key: roots["settings"].get(key)
            for key in ("storagePath", "emulatorAlternative", "bezels", "themeESDE")
            if key in roots["settings"]
        },
    }
    with _LAUNCHER_CACHE["lock"]:
        _LAUNCHER_CACHE["fingerprint"] = fingerprint
        _LAUNCHER_CACHE["data"] = data
    return data


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def invalidate_cache() -> None:
    with _LAUNCHER_CACHE["lock"]:
        _LAUNCHER_CACHE["fingerprint"] = None
        _LAUNCHER_CACHE["data"] = None
