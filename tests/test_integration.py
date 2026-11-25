"""Unit and integration tests for RomMArcade (stdlib unittest)."""

from __future__ import annotations

import io
import json
import os
import shutil
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys

sys.path.insert(0, ROOT)

from rommarcade import config as config_mod
from rommarcade.config import ConfigStore, normalize_url
from rommarcade.deeplink import DeepLinkQueue, parse_deeplink
from rommarcade.emudeck import ALIASES, choose_core, find_platform_dir, sanitize_name
from rommarcade.romm import RomMClient
from rommarcade.server import App, Server, Handler, _is_game_rom

TOKEN = "rmm_testtoken"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeRomM(BaseHTTPRequestHandler):
    server_version = "FakeRomM/5.4"
    approved = False

    def log_message(self, *args):  # quiet
        pass

    def _auth(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, status, data, content_type="application/octet-stream", filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/api/heartbeat":
            return self._json(200, {"SYSTEM": {"VERSION": "5.4.1-test"}})
        if path.startswith("/assets/"):
            return self._bytes(200, PNG, "image/png")
        if not path.startswith("/api/"):
            return self._json(404, {"detail": "not found"})
        if not self._auth():
            return self._json(401, {"detail": "Not authenticated"})
        if path == "/api/users/me":
            return self._json(200, {"id": 1, "username": "james", "role": "admin"})
        if path == "/api/platforms":
            return self._json(200, [
                {"id": 13, "name": "NES", "slug": "nes", "fs_slug": "nes", "display_name": "Nintendo Entertainment System", "rom_count": 2, "firmware_count": 1, "url_logo": "/assets/nes.png"},
                {"id": 22, "name": "Switch", "slug": "switch", "fs_slug": "switch", "display_name": "Nintendo Switch", "rom_count": 1, "firmware_count": 0, "url_logo": ""},
            ])
        if path == "/api/firmware":
            return self._json(200, [{"id": 1, "platform_id": 13, "file_name": "bios.bin", "file_path": "bios/nes", "file_size_bytes": 4, "full_path": "bios/nes/bios.bin"}])
        if path.startswith("/api/firmware/1/content/"):
            return self._bytes(200, b"BIOS", "application/octet-stream", "bios.bin")
        if path == "/api/roms":
            items = [ROM_NES, ROM_SWITCH]
            platform = query.get("platform_ids") or []
            if platform:
                items = [r for r in items if str(r["platform_id"]) in platform]
            wanted = query.get("rom_ids") or []
            if wanted:
                items = [r for r in items if str(r["id"]) in wanted]
            search = (query.get("search_term") or [""])[0].lower()
            if search:
                items = [r for r in items if search in r["name"].lower()]
            limit = int((query.get("limit") or ["50"])[0])
            offset = int((query.get("offset") or ["0"])[0])
            page = items[offset:offset + limit]
            if query.get("with_files") != ["true"]:
                page = [{k: v for k, v in r.items() if k != "files"} for r in page]
            return self._json(200, {"items": page, "total": len(items), "limit": limit, "offset": offset})
        if path == "/api/roms/180":
            return self._json(200, ROM_NES)
        if path == "/api/roms/181":
            return self._json(200, ROM_SWITCH)
        if path == "/api/roms/180/content/1942%20(Japan%2C%20USA).zip" or path == "/api/roms/180/content/1942 (Japan, USA).zip":
            return self._bytes(200, b"NES-ROM-DATA", "application/octet-stream", "1942 (Japan, USA).zip")
        if path.startswith("/api/roms/181/content/"):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as bundle:
                bundle.writestr("Game [0100].nsp", b"first")
                bundle.writestr("Game [0101].nsp", b"second")
            return self._bytes(200, buffer.getvalue(), "application/zip", "Game.nsp.zip")
        return self._json(404, {"detail": "no such endpoint " + path})

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/auth/device/init":
            return self._json(200, {
                "device_code": "dev-1",
                "user_code": "ABCD-1234",
                "verification_path": "/pair/device",
                "verification_path_complete": "/pair/device?user_code=ABCD-1234",
                "expires_in": 600,
                "interval": 5,
            })
        if path == "/api/auth/device/token":
            if body.get("device_code") != "dev-1":
                return self._json(400, {"detail": "expired_token"})
            if not FakeRomM.approved:
                return self._json(400, {"detail": "authorization_pending"})
            return self._json(200, {"id": 1, "access_token": TOKEN, "device_id": "dev", "scopes": ["roms.read"], "expires_at": None})
        return self._json(404, {"detail": "no such endpoint"})


ROM_NES = {
    "id": 180,
    "name": "1942",
    "platform_id": 13,
    "platform_slug": "nes",
    "platform_fs_slug": "nes",
    "platform_display_name": "Nintendo Entertainment System",
    "fs_size_bytes": 12,
    "has_multiple_files": False,
    "path_cover_large": "/assets/roms/13/180/cover/big.png",
    "path_cover_small": "/assets/roms/13/180/cover/small.png",
    "summary": "A vertical shooter.",
    "files": [{"file_name": "1942 (Japan, USA).zip", "file_size_bytes": 12, "is_top_level": True, "file_path": "roms/nes"}],
    "metadatum": {"genres": ["Shooter"], "player_count": "2"},
}

ROM_SWITCH = {
    "id": 181,
    "name": "Astral Chain",
    "platform_id": 22,
    "platform_slug": "switch",
    "platform_fs_slug": "switch",
    "platform_display_name": "Nintendo Switch",
    "fs_size_bytes": 20,
    "fs_name_no_ext": "Astral Chain",
    "has_multiple_files": True,
    "path_cover_large": "",
    "path_cover_small": "",
    "files": [{"file_name": "Game [0100].nsp", "file_size_bytes": 5, "is_top_level": True, "file_path": "roms/switch/Astral Chain"}],
}


class UnitTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(normalize_url("192.168.86.30:7676/"), "http://192.168.86.30:7676")
        self.assertEqual(normalize_url("http://x:1/api/"), "http://x:1")
        self.assertEqual(normalize_url("https://romm.example.com/romm/"), "https://romm.example.com/romm")
        with self.assertRaises(config_mod.ConfigError):
            normalize_url("ftp://nope")

    def test_secret_masking(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(os.path.join(tmp, "config.json"))
            store.add_server("http://example.test:1", {"mode": "token", "token": "rmm_secret"})
            masked = store.masked()
            self.assertNotIn("rmm_secret", json.dumps(masked))
            self.assertTrue(masked["servers"][0]["auth"]["has_secret"])

    def test_deeplink(self):
        target = parse_deeplink("rommarcade://open?rom=42&platform=nes&name=1942&play=1")
        self.assertEqual(target["rom"], 42)
        self.assertEqual(target["platform"], "nes")
        self.assertTrue(target["play"])
        with self.assertRaises(Exception):
            parse_deeplink("http://example.com")

    def test_deeplink_queue_consumes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = DeepLinkQueue(os.path.join(tmp, "deeplink.json"))
            queue.push({"rom": 1})
            self.assertEqual(queue.take(), {"rom": 1})
            self.assertIsNone(queue.take())

    def test_platform_aliases(self):
        self.assertEqual(ALIASES["ngc"], ["gc"])
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "gc"))
            self.assertEqual(find_platform_dir("ngc", tmp), "gc")
            self.assertEqual(find_platform_dir("n3ds", tmp), None)
        self.assertEqual(sanitize_name("A/B"), "A-B")
        self.assertEqual(choose_core("nes", {"mesen", "snes9x"}), "mesen")

    def test_game_filter(self):
        self.assertTrue(_is_game_rom({"platform_fs_slug": "nes", "files": [{"file_name": "1942.zip", "is_top_level": True}]}))
        self.assertFalse(_is_game_rom({"platform_fs_slug": "emulators", "files": [{"file_name": "azahar.sh", "is_top_level": True}]}))
        self.assertFalse(_is_game_rom({"platform_fs_slug": "nes", "files": []}))
        self.assertFalse(_is_game_rom({"platform_fs_slug": "xbox360", "files": [{"file_name": "xenia.sh", "is_top_level": True}]}))
        self.assertFalse(_is_game_rom({"platform_fs_slug": "xbox360", "files": [{"file_name": "LICENSE", "is_top_level": True}]}))
        self.assertFalse(_is_game_rom({"platform_fs_slug": "xbox360", "files": [{"file_name": "0b0a5c5c", "is_top_level": False}]}))


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rommarcade-test-")
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeRomM)
        cls.fake_port = cls.fake.server_address[1]
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()

        os.environ["ROMARCADE_HOME"] = os.path.join(cls.tmp, "home")
        os.environ["HOME"] = os.path.join(cls.tmp, "user")
        os.makedirs(os.path.join(os.environ["HOME"], ".var/app/org.libretro.RetroArch/config/retroarch/cores"), exist_ok=True)
        open(os.path.join(os.environ["HOME"], ".var/app/org.libretro.RetroArch/config/retroarch/cores/mesen_libretro.so"), "w").close()

        cls.emu = os.path.join(cls.tmp, "Emulation")
        for sub in ("roms/nes", "roms/switch", "bios", "tools/launchers"):
            os.makedirs(os.path.join(cls.emu, sub), exist_ok=True)
        launcher = os.path.join(cls.emu, "tools/launchers/retroarch.sh")
        cls.args_file = os.path.join(cls.tmp, "launcher-args.txt")
        with open(launcher, "w") as handle:
            handle.write(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {cls.args_file}\n")
        os.chmod(launcher, 0o755)

        cls.store = ConfigStore()
        cls.store.add_server(f"http://127.0.0.1:{cls.fake_port}", {"mode": "token", "token": TOKEN})
        cls.store.update({"emudeck": {"root": cls.emu}})
        cls.app = App(cls.store)
        cls.httpd = Server(("127.0.0.1", 0), Handler, cls.app)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.downloads.shutdown()
        cls.fake.shutdown()
        cls.fake.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("ROMARCADE_HOME", None)

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_01_status(self):
        status, payload = self.call("/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["configured"])
        self.assertTrue(payload["server"]["reachable"])
        self.assertEqual(payload["server"]["user"]["username"], "james")

    def test_02_platforms_mapping(self):
        status, payload = self.call("/api/platforms")
        self.assertEqual(status, 200)
        by = {p["fs_slug"]: p for p in payload["platforms"]}
        self.assertEqual(by["nes"]["local"]["dir"], "nes")
        self.assertEqual(by["nes"]["local"]["launcher"], "retroarch")
        self.assertEqual(by["nes"]["local"]["core"], "mesen")
        self.assertTrue(by["nes"]["local"]["exists"])
        self.assertTrue(by["nes"]["logo_url"].startswith("/api/platform-logo/"))

    def test_03_cover_proxy(self):
        request = urllib.request.Request(self.base + "/api/cover/180?size=small")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.read().startswith(b"\x89PNG"))

    def test_04_roms(self):
        status, payload = self.call("/api/roms?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        rom = [r for r in payload["items"] if r["id"] == 180][0]
        self.assertEqual(rom["platform_fs_slug"], "nes")
        self.assertTrue(rom["mapping"]["exists"])

    def test_05_install_and_download(self):
        status, payload = self.call("/api/install", "POST", {"rom_id": 180})
        self.assertEqual(status, 200, payload)
        deadline = time.time() + 15
        job = None
        while time.time() < deadline:
            _, downloads = self.call("/api/downloads")
            job = [j for j in downloads["downloads"] if j["rom_id"] == 180][0]
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "done", job)
        target = os.path.join(self.emu, "roms/nes/1942 (Japan, USA).zip")
        self.assertTrue(os.path.exists(target))
        self.assertEqual(open(target, "rb").read(), b"NES-ROM-DATA")
        _, installs = self.call("/api/installs")
        self.assertTrue(any(i["rom_id"] == 180 for i in installs["installs"]))
        _, detail = self.call("/api/roms/180")
        self.assertIsNotNone(detail["rom"]["installed"])

    def test_06_play_dry_run_and_spawn(self):
        status, payload = self.call("/api/play", "POST", {"rom_id": 180, "dry_run": True})
        self.assertEqual(status, 200, payload)
        self.assertIn("-L", payload["command"])
        self.assertIn("mesen", payload["command"])
        status, payload = self.call("/api/play", "POST", {"rom_id": 180})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["launched"])
        deadline = time.time() + 5
        while time.time() < deadline and not os.path.exists(self.args_file):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(self.args_file))
        args = open(self.args_file).read().splitlines()
        self.assertEqual(args[-1], os.path.join(self.emu, "roms/nes/1942 (Japan, USA).zip"))
        self.assertEqual(args[0], "-L")

    def test_07_multifile_zip(self):
        status, payload = self.call("/api/install", "POST", {"rom_id": 181})
        self.assertEqual(status, 200, payload)
        deadline = time.time() + 15
        target = os.path.join(self.emu, "roms/switch/Astral Chain")
        while time.time() < deadline:
            _, downloads = self.call("/api/downloads")
            job = [j for j in downloads["downloads"] if j["rom_id"] == 181][0]
            if job["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(sorted(os.listdir(target)), ["Game [0100].nsp", "Game [0101].nsp"])

    def test_08_bios(self):
        status, payload = self.call("/api/bios", "POST", {"platform_id": 13})
        self.assertEqual(status, 200, payload)
        deadline = time.time() + 15
        target = os.path.join(self.emu, "bios/nes/bios.bin")
        while time.time() < deadline and not os.path.exists(target):
            time.sleep(0.1)
        self.assertTrue(os.path.exists(target))
        self.assertEqual(open(target, "rb").read(), b"BIOS")

    def test_09_deeplink_http(self):
        status, payload = self.call("/api/deeplink", "POST", {"url": "rommarcade://open?rom=180&play=1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["target"]["rom"], 180)
        _, payload = self.call("/api/deeplink")
        self.assertEqual(payload["target"]["rom"], 180)
        _, payload = self.call("/api/deeplink")
        self.assertIsNone(payload["target"])

    def test_10_pairing(self):
        status, payload = self.call("/api/config/pair/start", "POST", {"url": self.base.replace(str(self.port), str(self.fake_port))})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["user_code"], "ABCD-1234")
        FakeRomM.approved = False
        _, pending = self.call("/api/config/pair/poll", "POST", {"device_code": payload["device_code"]})
        self.assertEqual(pending["status"], "pending")
        FakeRomM.approved = True
        _, approved = self.call("/api/config/pair/poll", "POST", {"device_code": payload["device_code"]})
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["user"]["username"], "james")

    def test_11_static_ui(self):
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read().decode()
        self.assertIn("<title>RomMArcade</title>", body)
        self.assertIn("/js/app.js", body)

    def test_12_log_and_debug(self):
        status, payload = self.call("/api/debug")
        self.assertEqual(status, 200)
        self.assertEqual(payload["product"], "RomMArcade")
        status, payload = self.call("/api/log?lines=5")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload["lines"], list)

    def test_13_emudeck_toggle(self):
        try:
            status, payload = self.call("/api/config", "PUT", {"emudeck": {"enabled": False}})
            self.assertEqual(status, 200)
            self.assertFalse(payload["config"]["emudeck"]["enabled"])
            _, status_payload = self.call("/api/status")
            self.assertFalse(status_payload["emudeck"]["enabled"])
            _, platforms = self.call("/api/platforms")
            self.assertFalse(next(p for p in platforms["platforms"] if p["fs_slug"] == "nes")["local"]["enabled"])
            code, payload = self.call("/api/install", "POST", {"rom_id": 180})
            self.assertEqual(code, 409)
            self.assertIn("off", payload["error"].lower())
        finally:
            self.call("/api/config", "PUT", {"emudeck": {"enabled": True}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
