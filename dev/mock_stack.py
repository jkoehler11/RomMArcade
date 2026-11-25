"""A throwaway RomM + EmuDeck stack for looking at the UI without a real server.

    python3 dev/mock_stack.py [--port 8791]

It serves a few dozen invented games with generated SVG covers, a fake EmuDeck
tree and fake launchers, then prints the RomMArcade URL.  Nothing is written
outside a temporary directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOKEN = "rmm_mocktoken"

PLATFORMS = [
    (13, "NES", "nes", "Nintendo Entertainment System", 20),
    (6, "SNES", "snes", "Super Nintendo Entertainment System", 18),
    (12, "GBA", "gba", "Game Boy Advance", 14),
    (14, "N64", "n64", "Nintendo 64", 9),
    (21, "PS2", "ps2", "PlayStation 2", 7),
    (4, "GameCube", "gc", "Nintendo GameCube", 8),
]

GAMES = {
    "nes": ["1942", "Super Mario Bros. 3", "Mega Man 2", "Contra", "Castlevania III", "Kirby's Adventure", "Metroid", "Punch-Out!!", "DuckTales", "Ninja Gaiden"],
    "snes": ["Super Metroid", "Chrono Trigger", "EarthBound", "Super Mario World", "Donkey Kong Country 2", "Final Fantasy VI", "ActRaiser", "Kirby Super Star"],
    "gba": ["Metroid Fusion", "Golden Sun", "Advance Wars", "Fire Emblem", "Castlevania: Aria of Sorrow", "Mother 3"],
    "n64": ["Super Mario 64", "The Legend of Zelda: Ocarina of Time", "Banjo-Kazooie", "Perfect Dark", "F-Zero X"],
    "ps2": ["Shadow of the Colossus", "Metal Gear Solid 3", "Katamari Damacy", "Okami", "Persona 3 FES"],
    "gc": ["Metroid Prime", "Luigi's Mansion", "F-Zero GX", "Paper Mario: The Thousand-Year Door"],
}

COLORS = [
    ("#39e6ff", "#0b1a2a"), ("#ff4fd8", "#1c0a18"), ("#ffc857", "#241c08"),
    ("#55f2a4", "#082018"), ("#8e9bff", "#11132b"), ("#ff7a59", "#24100a"),
]


def build_library():
    roms = []
    next_id = 100
    for platform_id, _name, slug, display, _count in PLATFORMS:
        for index, title in enumerate(GAMES[slug]):
            rid = next_id
            next_id += 1
            extension = ".iso" if slug in ("ps2", "gc") else ".zip"
            roms.append({
                "id": rid,
                "name": title,
                "platform_id": platform_id,
                "platform_slug": slug,
                "platform_fs_slug": slug,
                "platform_display_name": display,
                "fs_size_bytes": (index + 1) * 37 * 1024 * 1024 + rid * 1024,
                "has_multiple_files": False,
                "path_cover_large": f"/assets/roms/{rid}/cover.svg",
                "path_cover_small": f"/assets/roms/{rid}/cover.svg",
                "summary": "A mock entry so RomMArcade's library can be designed without touching a real server.",
                "metadatum": {"genres": ["Action"], "player_count": "1"},
                "files": [{"file_name": title.replace("/", "-") + extension, "file_size_bytes": 1024 * 1024, "is_top_level": True}],
                "_color": COLORS[rid % len(COLORS)],
            })
    return roms


ROMS = build_library()


def cover_svg(rom):
    a, b = rom["_color"]
    title = rom["name"]
    words = title.split()
    lines = []
    current = ""
    for word in words:
        if len(current) + len(word) > 14:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    tspans = "".join(
        f'<tspan x="200" y="{300 + i * 44}">{line.replace("&", "&amp;").replace("<", "&lt;")}</tspan>'
        for i, line in enumerate(lines[:4])
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="533" viewBox="0 0 400 533">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient></defs>
<rect width="400" height="533" fill="url(#g)"/>
<rect x="18" y="18" width="364" height="497" rx="12" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="2"/>
<text x="200" y="240" text-anchor="middle" fill="rgba(5,8,14,.85)" font-family="system-ui,sans-serif" font-size="46" font-weight="800">{rom["platform_slug"].upper()}</text>
<text x="200" text-anchor="middle" fill="#f4f7ff" font-family="system-ui,sans-serif" font-size="34" font-weight="750">{tspans}</text>
</svg>'''


class MockRomM(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/heartbeat":
            return self._json({"SYSTEM": {"VERSION": "5.4.1-mock"}})
        if path.startswith("/assets/"):
            try:
                rid = int(path.split("/")[3])
                rom = next(r for r in ROMS if r["id"] == rid)
            except Exception:
                return self._bytes(b"not found", "text/plain", 404)
            return self._bytes(cover_svg(rom).encode(), "image/svg+xml")
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._json({"detail": "Not authenticated"}, 401)
        if path == "/api/users/me":
            return self._json({"id": 1, "username": "james", "role": "admin"})
        if path == "/api/platforms":
            return self._json([
                {"id": pid, "name": name, "slug": slug, "fs_slug": slug, "display_name": display,
                 "rom_count": len(GAMES[slug]), "firmware_count": 4 if slug == "ps2" else 0,
                 "url_logo": ""}
                for pid, name, slug, display, _ in PLATFORMS
            ])
        if path == "/api/firmware":
            if (query.get("platform_id") or [""])[0] == "21":
                return self._json([
                    {"id": 1, "platform_id": 21, "file_name": "SCPH-70012_BIOS_V12_USA_200.BIN", "file_path": "bios/ps2", "file_size_bytes": 4 * 1024 * 1024},
                ])
            return self._json([])
        if path == "/api/roms":
            items = list(ROMS)
            platform = (query.get("platform_ids") or [""])[0]
            if platform:
                items = [r for r in items if str(r["platform_id"]) == platform]
            search = (query.get("search_term") or [""])[0].lower()
            if search:
                items = [r for r in items if search in r["name"].lower()]
            if (query.get("with_files") or ["false"])[0] != "true":
                items = [{k: v for k, v in r.items() if k not in ("files", "_color")} for r in items]
            limit = int((query.get("limit") or ["50"])[0])
            offset = int((query.get("offset") or ["0"])[0])
            return self._json({"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset})
        if path.startswith("/api/roms/"):
            parts = path.strip("/").split("/")
            try:
                rid = int(parts[2])
            except (IndexError, ValueError):
                return self._json({"detail": "bad id"}, 400)
            rom = next((r for r in ROMS if r["id"] == rid), None)
            if not rom:
                return self._json({"detail": "not found"}, 404)
            if len(parts) == 3:
                return self._json({k: v for k, v in rom.items() if k != "_color"})
            return self._bytes(b"MOCK-ROM-DATA", "application/octet-stream")
        if path == "/api/installs/repair":
            return self._json({"removed": 0})
        return self._json({"detail": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/auth/device/init":
            return self._json({
                "device_code": "mock-device", "user_code": "MOCK-1234",
                "verification_path": "/pair/device",
                "verification_path_complete": "/pair/device?user_code=MOCK-1234",
                "expires_in": 600, "interval": 5,
            })
        if path == "/api/auth/device/token":
            return self._json({"id": 1, "access_token": TOKEN, "device_id": "mock", "scopes": ["roms.read"], "expires_at": None})
        return self._json({"detail": "not found"}, 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="rommarcade-mock-")
    home = os.path.join(tmp, "user")
    emu = os.path.join(tmp, "Emulation")
    for sub in ("roms", "bios", "tools/launchers"):
        os.makedirs(os.path.join(emu, sub), exist_ok=True)
    for _pid, _name, slug, _display, _count in PLATFORMS:
        os.makedirs(os.path.join(emu, "roms", slug), exist_ok=True)
    cores = os.path.join(home, ".var/app/org.libretro.RetroArch/config/retroarch/cores")
    os.makedirs(cores, exist_ok=True)
    for core in ("mesen", "snes9x", "mgba", "mupen64plus_next", "fbneo", "swanstation"):
        open(os.path.join(cores, core + "_libretro.so"), "w").close()
    for launcher, flatpak in (
        ("retroarch", "org.libretro.RetroArch"),
        ("dolphin-emu", "org.DolphinEmu.dolphin-emu"),
        ("duckstation", "org.duckstation.DuckStation"),
        ("pcsx2-qt", "net.pcsx2.PCSX2"),
    ):
        path = os.path.join(emu, "tools/launchers", launcher + ".sh")
        with open(path, "w") as handle:
            handle.write(f"#!/bin/sh\n# fake launcher\nexec flatpak run {flatpak} \"$@\"\n")
        os.chmod(path, 0o755)

    os.environ["ROMARCADE_HOME"] = os.path.join(tmp, "state")
    os.environ["HOME"] = home

    fake = ThreadingHTTPServer(("127.0.0.1", 0), MockRomM)
    fake_port = fake.server_address[1]
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    from rommarcade.config import ConfigStore
    from rommarcade.server import App, Server, Handler

    store = ConfigStore()
    store.add_server(f"http://127.0.0.1:{fake_port}", {"mode": "token", "token": TOKEN}, "Mock RomM")
    store.update({"emudeck": {"root": emu}, "web": {"port": args.port}})
    app = App(store)
    httpd = Server(("127.0.0.1", args.port), Handler, app)
    print(f"RomMArcade mock stack on http://127.0.0.1:{args.port}  (RomM {fake_port}, EmuDeck {emu})")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        app.downloads.shutdown()
        httpd.server_close()
        fake.shutdown()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
