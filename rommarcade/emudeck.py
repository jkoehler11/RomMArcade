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
import shutil
import subprocess
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
    "xbox360": ["xenia-emu", "xenia"],
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
}

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
    """Return the EmuDeck trees, honouring explicit config overrides."""
    config = config or {}
    settings = _read_settings()
    storage = str(config.get("root") or settings.get("storagePath") or "").strip()
    candidates: list[str] = []
    if config.get("root"):
        candidates.append(str(config["root"]))
    if storage:
        candidates.append(os.path.join(storage, "Emulation"))
        candidates.append(storage)
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
            "detected": False,
            "storage_path": storage,
            "root": "",
            "roms_root": "",
            "bios_root": "",
            "launchers_root": "",
            "settings": settings,
        }
    roms = str(config.get("roms_root") or os.path.join(emulation_root, "roms"))
    bios = str(config.get("bios_root") or os.path.join(emulation_root, "bios"))
    launchers = str(
        config.get("launchers_root") or os.path.join(emulation_root, "tools", "launchers")
    )
    return {
        "detected": True,
        "storage_path": storage or os.path.dirname(emulation_root),
        "root": emulation_root,
        "roms_root": roms,
        "bios_root": bios,
        "launchers_root": launchers,
        "settings": settings,
    }


def _script_backend(path: str) -> str:
    """What a launcher needs: a flatpak id, an AppImage path, or nothing."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(20000)
    except OSError:
        return ""
    match = re.search(r"flatpak\s+run\s+([A-Za-z0-9_.-]+)", text)
    if match:
        return "flatpak:" + match.group(1)
    for line in text.splitlines():
        if "AppImage" in line:
            for token in re.findall(r"[^\s\"']+\.AppImage", line):
                if token.startswith("/"):
                    return "appimage:" + token
    return ""


def _backend_ready(backend: str, stem: str = "") -> bool:
    if not backend:
        return _launcher_binary_present(stem)
    kind, _, value = backend.partition(":")
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


def discover_launchers(launchers_root: str, available_cores: set[str]) -> list[dict[str, Any]]:
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
        backend = _script_backend(real)
        seen[stem] = {
            "id": stem,
            "name": EMULATOR_LABELS.get(stem, stem.replace("-", " ").title()),
            "path": path,
            "real_path": real,
            "backend": backend,
            "ready": _backend_ready(backend, stem),
        }
    return list(seen.values())


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
    candidates = list(STANDALONE.get(fs_slug, []))
    for candidate in candidates:
        if candidate in ready:
            return ready[candidate]
    if "retroarch" in ready:
        if cores is None or choose_core(fs_slug, cores):
            return ready["retroarch"]
        if fs_slug not in STANDALONE:
            return ready["retroarch"]
    return None


def choose_core(fs_slug: str, cores: set[str]) -> str | None:
    for candidate in CORES.get(fs_slug, []):
        if candidate in cores:
            return candidate
    return None


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
    core = override.get("core")
    if core is None:
        core = choose_core(fs_slug, set(cores)) if (launcher or {}).get("id") == "retroarch" else None
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
        "core": core or "",
        "source": "override" if override else ("alias" if directory and directory.lower() != fs_slug.lower() else "catalogue"),
    }


def rom_file(rom: dict[str, Any]) -> dict[str, Any] | None:
    files = rom.get("files") or []
    top = [f for f in files if f.get("is_top_level")] or files
    if not top:
        return None
    return top[0]


def rom_target_name(rom: dict[str, Any]) -> str:
    if rom.get("has_multiple_files"):
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
            f"EmuDeck has no folder for {mapping.get('name') or 'this platform'}. "
            "Choose one in Settings, Emulators."
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
    if mapping.get("launcher") == "retroarch" and mapping.get("core"):
        command += ["-L", mapping["core"]]
    command.append(file_path)
    return command


def spawn(command: list[str], log_file: str, env: dict[str, str] | None = None) -> int:
    """Start an emulator detached from the app, in the user's GUI session."""
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
    return process.pid


def detect(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full EmuDeck snapshot, cheap enough to cache for a few seconds."""
    config = config or {}
    fingerprint = json.dumps(
        {
            "settings_mtime": _mtime(emudeck_settings_path()),
            "root": config.get("root"),
            "roms": config.get("roms_root"),
            "launchers": config.get("launchers_root"),
        },
        sort_keys=True,
    )
    with _LAUNCHER_CACHE["lock"]:
        if _LAUNCHER_CACHE["fingerprint"] == fingerprint and _LAUNCHER_CACHE["data"] is not None:
            return _LAUNCHER_CACHE["data"]
    roots = resolve_roots(config)
    cores = discover_cores()
    launchers = discover_launchers(roots["launchers_root"], set(cores)) if roots["detected"] else []
    rom_dirs = []
    if roots["detected"] and os.path.isdir(roots["roms_root"]):
        try:
            rom_dirs = sorted(
                name
                for name in os.listdir(roots["roms_root"])
                if os.path.isdir(os.path.join(roots["roms_root"], name))
            )
        except OSError:
            rom_dirs = []
    data = {
        "detected": roots["detected"],
        "storage_path": roots["storage_path"],
        "root": roots["root"],
        "roms_root": roots["roms_root"],
        "bios_root": roots["bios_root"],
        "launchers_root": roots["launchers_root"],
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
