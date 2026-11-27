"""Unit and integration tests for RomMArcade (stdlib unittest)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socketserver
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys

sys.path.insert(0, ROOT)

from rommarcade import config as config_mod
from rommarcade.config import ConfigStore, normalize_url
from rommarcade.deeplink import DeepLinkQueue, parse_deeplink
from rommarcade.emudeck import (
    ALIASES,
    EmuDeckError,
    build_command,
    choose_core,
    choose_launcher,
    create_library,
    discover_launchers,
    find_platform_dir,
    library_roots,
    mark_superseded,
    plan_library,
    platform_choices,
    is_multi_file,
    missing_emulator_bios,
    bios_status,
    repair_emulator_bios,
    storage_status,
    migration_move,
    migration_snapshot,
    rom_target_name,
    platform_mapping,
    resolve_roots,
    sanitize_name,
)
from rommarcade.romm import RomMClient
from rommarcade.saves import SyncState, local_saves
from rommarcade.server import App, Server, Handler, _is_game_rom, collapse_sibling_roms

TOKEN = "rmm_testtoken"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class FakeRomM(BaseHTTPRequestHandler):
    server_version = "FakeRomM/5.4"
    approved = False
    # Save-sync simulation: absent (no API), download, upload, conflict, noop.
    save_mode = "absent"
    save_bytes = b"SAVE-FROM-SERVER"
    uploaded = []
    upload_queries = []
    confirmed = []
    deleted = []
    flags = []
    firmware_uploads = []
    firmware_deleted = []

    @classmethod
    def reset_saves(cls, mode="download"):
        cls.save_mode = mode
        cls.save_bytes = b"SAVE-FROM-SERVER"
        cls.uploaded = []
        cls.upload_queries = []
        cls.confirmed = []
        cls.deleted = []
        cls.flags = []

    @classmethod
    def save_row(cls, **overrides):
        row = {
            "id": 900,
            "rom_id": 180,
            "file_name": "1942 (Japan, USA) [2026-11-26_12-00-00-000].srm",
            "slot": "autosave",
            "emulator": "mesen",
            "file_size_bytes": len(cls.save_bytes),
            "content_hash": hashlib.md5(cls.save_bytes, usedforsecurity=False).hexdigest(),
            "updated_at": "2026-11-26T12:00:00+00:00",
            "created_at": "2026-11-26T11:00:00+00:00",
            "is_favorite": False,
            "labels": [],
            "is_public": False,
            "missing_from_fs": False,
        }
        row.update(overrides)
        return row

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
        if path == "/api/saves":
            if query.get("rom_id") and query["rom_id"][0] != "180":
                return self._json(200, [])
            rows = [self.save_row()] if self.save_mode != "absent" else []
            for entry in reversed(self.uploaded):
                rows.insert(0, entry["row"])
            return self._json(200, rows)
        if path == "/api/saves/900":
            return self._json(200, self.save_row())
        if path.startswith("/api/saves/900/content"):
            return self._bytes(200, self.save_bytes, "application/octet-stream",
                               "1942 (Japan, USA) [tagged].srm")
        if path.startswith("/api/saves/901/content"):
            entry = next((e for e in self.uploaded if e["row"]["id"] == 901), None)
            if not entry:
                return self._json(404, {"detail": "no such save"})
            return self._bytes(200, entry["data"], "application/octet-stream", entry["name"])
        return self._json(404, {"detail": "no such endpoint " + path})

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        original_raw = raw
        content_type = self.headers.get("Content-Type") or ""
        if "multipart/form-data" in content_type:
            body = {}
            boundary = content_type.split("boundary=", 1)[-1].encode()
            marker = b"\r\n\r\n"
            head, _, rest = raw.partition(marker)
            data = rest.split(b"\r\n--" + boundary, 1)[0] if rest else b""
            name = ""
            for part in head.split(b";"):
                if b"filename=" in part:
                    name = part.split(b"filename=", 1)[1].decode("utf-8", "replace").strip().strip('"')
            raw = (name, data)
        else:
            body = json.loads(raw or b"{}")
        if path == "/api/firmware" and "multipart/form-data" in content_type:
            self.firmware_uploads.append({"raw": original_raw, "query": query})
            return self._json(200, {"uploaded": [{"file_name": "bios.bin"}]})
        if path == "/api/firmware/delete":
            self.firmware_deleted.append(body)
            return self._json(200, {"success": True})
        if path == "/api/sync/negotiate":
            if self.save_mode == "absent":
                return self._json(404, {"detail": "no such endpoint " + path})
            saves = body.get("saves") or []
            match = next((s for s in saves if s.get("slot") == "autosave"), None)
            server_hash = hashlib.md5(self.save_bytes, usedforsecurity=False).hexdigest()
            if self.save_mode == "download":
                operations = [] if match and match.get("content_hash") == server_hash else [{
                    "action": "download", "rom_id": 180, "save_id": 900,
                    "file_name": "1942 (Japan, USA) [tagged].srm", "slot": "autosave",
                    "emulator": "mesen", "reason": "Server has a newer save",
                    "server_updated_at": "2026-11-26T12:00:00+00:00",
                    "server_content_hash": server_hash,
                }]
            elif self.save_mode == "conflict":
                operations = [{
                    "action": "conflict", "rom_id": 180, "save_id": 900,
                    "file_name": "1942 (Japan, USA) [tagged].srm", "slot": "autosave",
                    "emulator": "mesen", "reason": "Changed on both sides",
                    "server_updated_at": "2026-11-26T12:00:00+00:00",
                    "server_content_hash": server_hash,
                }]
            elif self.save_mode == "upload":
                operations = [{
                    "action": "upload", "rom_id": 180, "slot": "autosave",
                    "file_name": (match or {}).get("file_name") or "1942.srm",
                    "emulator": "mesen", "reason": "Local save changed",
                }]
            else:
                operations = []
            return self._json(200, {
                "session_id": 1, "operations": operations,
                "total_upload": 0, "total_download": 0, "total_conflict": 0,
                "total_no_op": 0, "total_delete": 0,
            })
        if path == "/api/devices":
            return self._json(201, {"device_id": "dev-test", "name": "test", "created_at": "now"})
        if path == "/api/saves":
            name, data = raw if isinstance(raw, tuple) else ("save.srm", b"")
            digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
            slot = (query.get("slot") or ["autosave"])[0]
            overwrite = (query.get("overwrite") or ["false"])[0] == "true"
            self.upload_queries.append({"slot": slot, "overwrite": overwrite,
                                        "content_hash": digest, "name": name})
            if not overwrite:
                # RomM promotes a version that already holds these bytes instead
                # of storing an identical copy again.
                rows = [self.save_row()] + [entry["row"] for entry in self.uploaded]
                existing = next((r for r in rows if r["slot"] == slot
                                 and r["content_hash"] == digest), None)
                if existing:
                    return self._json(200, existing)
            row = self.save_row(
                id=901 + len(self.uploaded), file_name=name or "save.srm", slot=slot,
                file_size_bytes=len(data), content_hash=digest,
                is_favorite=False, labels=[], is_public=False,
            )
            self.uploaded.append({"row": row, "data": data, "name": name or "save.srm"})
            return self._json(200, row)
        if path == "/api/saves/900/downloaded":
            self.confirmed.append(body)
            return self._json(200, self.save_row())
        if path == "/api/saves/delete":
            self.deleted.extend(body.get("saves") or [])
            return self._json(200, body.get("saves") or [])
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

    def do_PUT(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.flags.append((path, body))
        overrides = {}
        if path.endswith("/favorite"):
            overrides["is_favorite"] = bool(body.get("is_favorite"))
        elif path.endswith("/visibility"):
            overrides["is_public"] = bool(body.get("is_public"))
        elif path.endswith("/file-name"):
            overrides["file_name"] = body.get("file_name")
        elif path.endswith("/labels"):
            overrides["labels"] = body.get("labels") or []
        return self._json(200, self.save_row(**overrides))


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

    def test_platform_config_dirs(self):
        with patch.dict(os.environ, {"ROMARCADE_HOME": ""}):
            with patch("rommarcade.config.sys.platform", "darwin"):
                self.assertTrue(config_mod.config_dir().endswith("Library/Application Support/RomMArcade"))
                self.assertTrue(config_mod.data_dir().endswith("Library/Application Support/RomMArcade"))
            with patch("rommarcade.config.sys.platform", "win32"), patch.dict(
                os.environ,
                {"APPDATA": "C:/Users/test/AppData/Roaming", "LOCALAPPDATA": "C:/Users/test/AppData/Local"},
            ):
                self.assertTrue(config_mod.config_dir().endswith("AppData/Roaming/RomMArcade"))
                self.assertTrue(config_mod.data_dir().endswith("AppData/Local/RomMArcade"))

    def test_library_plan_and_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_library("romm", tmp)
            self.assertEqual(plan["roms_root"], os.path.join(tmp, "roms"))
            self.assertEqual(plan["bios_root"], os.path.join(tmp, "bios"))
            result = create_library(
                plan,
                [
                    {"fs_slug": "nes", "game_count": 2},
                    {"fs_slug": "switch", "game_count": 1},
                    {"fs_slug": "empty", "game_count": 0},
                ],
            )
            self.assertEqual(sorted(result["created"]), ["nes", "switch"])
            self.assertTrue(os.path.isdir(os.path.join(tmp, "roms", "nes")))
            self.assertTrue(os.path.isdir(os.path.join(tmp, "bios")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "roms", "empty")))
            # An existing alias-named folder is reused, never duplicated.
            os.makedirs(os.path.join(tmp, "roms", "psx"))
            result = create_library(plan, [{"fs_slug": "ps1", "game_count": 5}])
            self.assertEqual(result["created"], [])
            row = [item for item in result["platforms"] if item["platform"] == "ps1"][0]
            self.assertEqual(row["dir"], "psx")

    def test_library_roots_and_validation(self):
        state = library_roots({"library": {"mode": "custom", "root": "/games/roms"}})
        self.assertEqual(state["roms_root"], "/games/roms")
        self.assertEqual(state["bios_root"], "/games/bios")
        state = library_roots({"library": {"mode": "romm", "root": "/games"}})
        self.assertEqual(state["roms_root"], "/games/roms")
        self.assertEqual(state["bios_root"], "/games/bios")
        with self.assertRaises(Exception):
            plan_library("romm", "relative/folder")
        with self.assertRaises(Exception):
            plan_library("romm", "/")
        with self.assertRaises(Exception):
            plan_library("emudeck", "/games")

    def test_library_overrides_emudeck_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            emu = os.path.join(tmp, "Emulation")
            os.makedirs(os.path.join(emu, "roms"))
            library = os.path.join(tmp, "RomM")
            os.makedirs(os.path.join(library, "roms"))
            roots = resolve_roots(
                {"emudeck": {"root": emu}, "library": {"mode": "romm", "root": library}}
            )
            self.assertEqual(roots["mode"], "library")
            self.assertEqual(roots["roms_root"], os.path.join(library, "roms"))
            self.assertFalse(roots["detected"])

    def test_platform_choices_only_valid_emulators(self):
        launchers = [
            {"id": "retroarch", "name": "RetroArch", "ready": True},
            {"id": "duckstation", "name": "DuckStation", "ready": True},
            {"id": "xenia", "name": "Xenia", "ready": True},
            {"id": "xenia-emu", "name": "Xenia", "ready": False, "superseded": True},
            {"id": "melonds", "name": "melonDS", "ready": False},
        ]
        cores = ["mesen", "snes9x"]
        nes = platform_choices("nes", launchers, cores)
        self.assertEqual(nes["launchers"], ["retroarch"])
        self.assertEqual(nes["cores"], ["mesen"])
        xbox = platform_choices("xbox360", launchers, cores)
        self.assertEqual(xbox["launchers"], ["xenia"])
        self.assertEqual(xbox["cores"], [])
        psx = platform_choices("psx", launchers, cores)
        self.assertEqual(psx["launchers"], ["duckstation"])
        self.assertEqual(psx["cores"], [])
        switch = platform_choices("switch", launchers, cores)
        self.assertEqual(switch["launchers"], [], "no Switch emulator is installed, so none is offered")
        unknown = platform_choices("mystery", launchers, cores)
        self.assertIn("retroarch", unknown["launchers"])
        self.assertEqual(unknown["cores"], ["mesen", "snes9x"])

    def test_single_archive_game_is_a_folder(self):
        jsrf = {
            "id": 260,
            "name": "Jet Set Radio Future",
            "has_multiple_files": False,
            "files": [
                {"file_name": "default.xbe", "is_top_level": False},
                {"file_name": "s_set_01a.adx", "is_top_level": False},
            ],
        }
        self.assertTrue(is_multi_file(jsrf), "an archive of many files is a tree even when RomM says otherwise")
        self.assertEqual(rom_target_name(jsrf), "Jet Set Radio Future")
        single = {
            "id": 180,
            "has_multiple_files": False,
            "files": [{"file_name": "1942 (Japan, USA).zip", "is_top_level": True}],
        }
        self.assertFalse(is_multi_file(single))
        self.assertEqual(rom_target_name(single), "1942 (Japan, USA).zip")

    def test_xbox_uses_xemu_not_retroarch(self):
        launchers = [
            {"id": "retroarch", "name": "RetroArch", "ready": True},
            {"id": "xemu-emu", "name": "xemu", "ready": True},
        ]
        choices = platform_choices("xbox", launchers, ["mesen"])
        self.assertEqual(choices["launchers"], ["xemu-emu"])
        self.assertEqual(choices["cores"], [])
        chosen = choose_launcher("xbox", launchers, {"mesen"})
        self.assertEqual(chosen["id"], "xemu-emu")
        only_retroarch = [{"id": "retroarch", "name": "RetroArch", "ready": True}]
        stub = choose_launcher("xbox", only_retroarch, {"mesen"}) or {}
        self.assertEqual(stub.get("id"), "xemu-emu")
        self.assertTrue(stub.get("stub"))
        self.assertFalse(stub.get("ready"))
        self.assertEqual(platform_choices("xbox", only_retroarch, ["mesen"])["launchers"], [])

    def test_save_files_are_not_games(self):
        srm = {
            "platform_fs_slug": "n64",
            "files": [{"file_name": "Super Mario 64 (USA).srm", "is_top_level": True}],
        }
        self.assertFalse(_is_game_rom(srm))
        rom = {
            "platform_fs_slug": "n64",
            "files": [{"file_name": "Super Mario 64 (USA).7z", "is_top_level": True}],
        }
        self.assertTrue(_is_game_rom(rom))

    def test_sibling_collapse_is_stem_aware(self):
        twisted_chd = {
            "id": 192, "name": "Twisted Metal: Black", "fs_size_bytes": 3405164262,
            "files": [{"file_name": "SCUS-97101 (1.00).chd", "is_top_level": True}],
            "sibling_roms": [{"id": 565}],
        }
        twisted_zip = {
            "id": 565, "name": "Twisted Metal: Black", "fs_size_bytes": 2933733719,
            "files": [{"file_name": "Twisted Metal_ Black.zip", "is_top_level": True}],
            "sibling_roms": [{"id": 192}],
        }
        out = collapse_sibling_roms([twisted_chd, twisted_zip])
        visible = [rom for rom in out if not rom.get("hidden_sibling")]
        self.assertEqual([rom["id"] for rom in visible], [192], "the .chd is the representative")
        self.assertEqual(visible[0]["version_count"], 2)
        self.assertEqual({entry["id"] for entry in visible[0]["versions"]}, {192, 565})
        hidden = [rom for rom in out if rom.get("hidden_sibling")][0]
        self.assertEqual(hidden["id"], 565)
        self.assertEqual(hidden["version_of"], 192)
        self.assertEqual(hidden["version_count"], 2, "the hidden page still offers the picker")

        allstars = {
            "id": 210, "name": "Super Mario RPG: Legend of the Seven Stars",
            "files": [{"file_name": "Super Mario All-Stars (USA).zip", "is_top_level": True}],
            "sibling_roms": [{"id": 212}],
        }
        rpg = {
            "id": 212, "name": "Super Mario RPG: Legend of the Seven Stars",
            "files": [{"file_name": "Super Mario RPG - Legend of the Seven Stars (USA).zip", "is_top_level": True}],
            "sibling_roms": [{"id": 210}],
        }
        pokemon_3ds = {
            "id": 493, "name": "Pokémon X",
            "files": [{"file_name": "Pokemon X (USA) (En,Ja,Fr,De,Es,It,Ko) Decrypted.3ds", "is_top_level": True}],
            "sibling_roms": [{"id": 497}],
        }
        pokemon_zip = {
            "id": 497, "name": "Pokémon X",
            "files": [{"file_name": "Pokemon X (USA) (EnJaFrDeEsItKo).7z", "is_top_level": True}],
            "sibling_roms": [{"id": 493}],
        }
        out = collapse_sibling_roms([pokemon_3ds, pokemon_zip])
        visible = [rom for rom in out if not rom.get("hidden_sibling")]
        self.assertEqual([rom["id"] for rom in visible], [493], "region tags do not defeat the stem match")
        self.assertEqual(visible[0]["version_count"], 2)

        out = collapse_sibling_roms([allstars, rpg])
        self.assertEqual(
            sorted(rom["id"] for rom in out if not rom.get("hidden_sibling")),
            [210, 212],
            "a bad IGDB match must not hide a different game",
        )

    def test_xemu_reports_missing_bios(self):
        with tempfile.TemporaryDirectory() as tmp:
            bios = os.path.join(tmp, "bios")
            os.makedirs(bios)
            self.assertEqual(
                missing_emulator_bios("xemu-emu", bios),
                ["mcpx_1.0.bin", "Complex_4627v1.03.bin"],
            )
            mapping = platform_mapping(
                {"fs_slug": "xbox", "slug": "xbox"},
                {"roms_root": os.path.join(tmp, "roms"), "bios_root": bios},
                [{"id": "xemu-emu", "name": "xemu", "ready": True, "path": "/xemu"}],
                [],
                {},
            )
            self.assertEqual(mapping["launcher"], "xemu-emu")
            self.assertEqual(mapping["bios_required"], ["mcpx_1.0.bin", "Complex_4627v1.03.bin"])
            self.assertEqual(mapping["bios_missing"], ["mcpx_1.0.bin", "Complex_4627v1.03.bin"])
            with open(os.path.join(bios, "MCPX_1.0.BIN"), "wb") as handle:
                handle.write(b"\0" * 512)
            with open(os.path.join(bios, "Complex_4627v1.03.bin"), "wb") as handle:
                handle.write(b"\0" * (1024 * 1024))
            self.assertEqual(missing_emulator_bios("xemu-emu", bios), [], "names match case-insensitively")
            self.assertEqual(missing_emulator_bios("retroarch", bios), [])

    def test_migration_moves_missing_files_into_the_active_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = os.path.join(tmp, "Emulation")
            active = os.path.join(outer, "Emulation")
            os.makedirs(os.path.join(outer, "bios"))
            os.makedirs(os.path.join(outer, "roms", "nes"))
            os.makedirs(os.path.join(active, "bios"))
            os.makedirs(os.path.join(active, "roms"))
            with open(os.path.join(outer, "bios", "bios.bin"), "wb") as handle:
                handle.write(b"bios")
            with open(os.path.join(outer, "roms", "nes", "game.nes"), "wb") as handle:
                handle.write(b"game")
            roots = {
                "root": active, "bios_root": os.path.join(active, "bios"),
                "roms_root": os.path.join(active, "roms"),
            }
            with patch("rommarcade.emudeck.resolve_roots", return_value=roots):
                plan = migration_snapshot({"emudeck": {"root": tmp}})
                self.assertEqual(plan["totals"]["move_files"], 2)
                result = migration_move({"emudeck": {"root": tmp}})
            self.assertEqual(result["moved_files"], 2)
            self.assertTrue(os.path.isfile(os.path.join(active, "bios", "bios.bin")))
            self.assertTrue(os.path.isfile(os.path.join(active, "roms", "nes", "game.nes")))
            self.assertFalse(os.path.exists(os.path.join(outer, "bios", "bios.bin")))

    def test_dreamcast_bios_requirements_are_platform_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "dreamcast")
            os.makedirs(folder)
            with open(os.path.join(folder, "dc_boot.bin"), "wb") as handle:
                handle.write(b"\0" * (2 * 1024 * 1024))
            with open(os.path.join(folder, "dc_flash.bin"), "wb") as handle:
                handle.write(b"\0" * (128 * 1024))
            status = {entry["name"]: entry["status"] for entry in bios_status("flycast", tmp, "dreamcast")}
            self.assertEqual(status, {"dc_boot.bin": "present", "dc_flash.bin": "present"})
            self.assertEqual(missing_emulator_bios("flycast", tmp, "dreamcast"), [])
            root_only = os.path.join(tmp, "root-only")
            os.makedirs(root_only)
            with open(os.path.join(root_only, "dc_boot.bin"), "wb") as handle:
                handle.write(b"\0" * (2 * 1024 * 1024))
            misplaced = {entry["name"]: entry for entry in bios_status("flycast", root_only, "dreamcast")}
            self.assertEqual(misplaced["dc_boot.bin"]["status"], "misplaced")
            self.assertTrue(misplaced["dc_boot.bin"]["can_repair"])

    def test_storage_status_reads_the_xemu_disk_and_eeprom(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = os.path.join(tmp, "storage", "xemu")
            os.makedirs(storage)
            missing = storage_status("xemu-emu", os.path.join(tmp, "storage"))
            self.assertEqual({entry["kind"]: entry["status"] for entry in missing},
                             {"hdd": "missing", "eeprom": "missing"})
            header = bytearray(64)
            header[:4] = b"QFI\xfb"
            header[4:8] = (3).to_bytes(4, "big")
            header[24:32] = (8 * 1024 ** 3).to_bytes(8, "big")
            with open(os.path.join(storage, "xbox_hdd.qcow2"), "wb") as handle:
                handle.write(bytes(header))
            with open(os.path.join(storage, "eeprom.bin"), "wb") as handle:
                handle.write(b"\0" * 256)
            status = {entry["kind"]: entry for entry in storage_status("xemu-emu", os.path.join(tmp, "storage"))}
            self.assertEqual(status["hdd"]["status"], "present")
            self.assertEqual(status["hdd"]["virtual_size"], 8 * 1024 ** 3)
            self.assertEqual(status["eeprom"]["status"], "present")

    def test_xemu_launches_an_iso_with_cdrom(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = os.path.join(tmp, "xemu-emu.sh")
            with open(launcher, "w") as handle:
                handle.write("")
            os.chmod(launcher, 0o755)
            game = os.path.join(tmp, "Jet Set Radio Future")
            os.makedirs(game)
            iso = os.path.join(game, "rommarcade.iso")
            with open(iso, "w") as handle:
                handle.write("")
            command = build_command(
                {"launcher": "xemu-emu", "launcher_path": launcher, "name": "Xbox"}, game
            )
            self.assertEqual(command[-2:], ["-cdrom", iso])
            bare = os.path.join(tmp, "loose")
            os.makedirs(bare)
            with self.assertRaises(EmuDeckError):
                build_command(
                    {"launcher": "xemu-emu", "launcher_path": launcher, "name": "Xbox"}, bare
                )

    def test_bios_status_finds_misplaced_and_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bios = os.path.join(tmp, "bios")
            platform = os.path.join(bios, "xbox")
            os.makedirs(platform)
            with open(os.path.join(platform, "Complex_4627.bin"), "wb") as handle:
                handle.write(b"\0" * (1024 * 1024))
            with open(os.path.join(platform, "mcpx_1.0.bin"), "wb") as handle:
                handle.write(b"\0" * 100)
            status = {entry["name"]: entry for entry in bios_status("xemu-emu", bios, "xbox")}
            self.assertEqual(status["Complex_4627v1.03.bin"]["status"], "misplaced")
            self.assertTrue(status["Complex_4627v1.03.bin"]["can_repair"])
            self.assertEqual(status["mcpx_1.0.bin"]["status"], "invalid")
            self.assertEqual(status["mcpx_1.0.bin"]["actual_size"], 100)
            result = repair_emulator_bios("xemu-emu", bios, "xbox")
            self.assertEqual([item["name"] for item in result["copied"]], ["Complex_4627v1.03.bin"])
            self.assertTrue(os.path.isfile(os.path.join(bios, "Complex_4627v1.03.bin")))
            self.assertEqual(result["failed"][0]["name"], "mcpx_1.0.bin")
            self.assertIn("expected", result["failed"][0]["reason"])

    def test_storage_base_follows_emudeck_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = os.path.join(tmp, "Emulation")
            nested = os.path.join(outer, "Emulation")
            for root in (outer, nested):
                os.makedirs(os.path.join(root, "roms", "nes"))
            with mock.patch("rommarcade.emudeck._read_settings", return_value={"storagePath": outer}):
                roots = resolve_roots({"emudeck": {"root": tmp}})
                self.assertEqual(
                    roots["root"], nested,
                    "a storage base must resolve to the tree EmuDeck's launchers use",
                )
                explicit = resolve_roots({"emudeck": {"root": outer}})
                self.assertEqual(explicit["root"], outer, "an explicit Emulation folder wins")

    def test_superseded_duplicate_launcher(self):
        launchers = [
            {"id": "xenia", "name": "Xenia", "ready": True},
            {"id": "xenia-emu", "name": "Xenia", "ready": False},
            {"id": "melonds", "name": "melonDS", "ready": False},
        ]
        flags = {l["id"]: l["superseded"] for l in mark_superseded(launchers)}
        self.assertFalse(flags["xenia"])
        self.assertTrue(flags["xenia-emu"], "a dead variant beside a working launcher is not missing")
        self.assertFalse(flags["melonds"], "a lone dead launcher is still a missing emulator")

    def test_proton_launcher_readiness(self):
        """EmuDeck's Xenia is a Proton .exe; it is installed without an AppImage."""
        with tempfile.TemporaryDirectory() as tmp:
            emu = os.path.join(tmp, "Emulation")
            launchers = os.path.join(emu, "tools", "launchers")
            xbox = os.path.join(emu, "roms", "xbox360")
            os.makedirs(launchers)
            os.makedirs(xbox)
            exe = os.path.join(xbox, "xenia_canary.exe")
            with open(exe, "wb") as handle:
                handle.write(b"MZ")
            script = os.path.join(launchers, "xenia.sh")
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/usr/bin/env bash\n"
                    '. "$HOME/.config/EmuDeck/backend/functions/all.sh"\n'
                    'PROTONLAUNCH="${toolsPath}/proton-launch.sh"\n'
                    'XENIA="$romsPath/xbox360/xenia_canary.exe"\n'
                    '"${PROTONLAUNCH}" -p "Experimental" -- "${XENIA}" "${@}"\n'
                )
            os.chmod(script, 0o755)
            emu_script = os.path.join(launchers, "xenia-emu.sh")
            with open(emu_script, "w", encoding="utf-8") as handle:
                handle.write(
                    "#!/usr/bin/env bash\n"
                    'XENIA="$HOME/Applications/NoSuchXeniaVariant.AppImage"\n'
                    '"$XENIA" "${@}"\n'
                )
            os.chmod(emu_script, 0o755)
            found = discover_launchers(launchers, set(), emu)
            entry = [item for item in found if item["id"] == "xenia"][0]
            self.assertTrue(entry["ready"], entry)
            self.assertTrue(entry["backend"].startswith("proton:"))
            self.assertFalse([item for item in found if item["id"] == "xenia-emu"][0]["ready"])
            chosen = choose_launcher("xbox360", found) or {}
            self.assertEqual(chosen.get("id"), "xenia")
            mapping = platform_mapping(
                {"fs_slug": "xbox360", "slug": "xbox360"},
                {"roms_root": os.path.join(emu, "roms")},
                found,
                [],
                {"xbox360": {"dir": "xbox360", "launcher": "xenia-emu"}},
            )
            self.assertEqual(mapping["launcher"], "xenia")
            self.assertTrue(mapping["launcher_ready"])
            os.unlink(exe)
            found = discover_launchers(launchers, set(), emu)
            entry = [item for item in found if item["id"] == "xenia"][0]
            self.assertFalse(entry["ready"], entry)


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
        self.assertIn("installer", payload["emudeck"])

    def test_02_platforms_mapping(self):
        status, payload = self.call("/api/platforms")
        self.assertEqual(status, 200)
        by = {p["fs_slug"]: p for p in payload["platforms"]}
        self.assertEqual(by["nes"]["local"]["dir"], "nes")
        self.assertEqual(by["nes"]["local"]["launcher"], "retroarch")
        self.assertEqual(by["nes"]["local"]["core"], "mesen")
        self.assertTrue(by["nes"]["local"]["exists"])
        self.assertTrue(by["nes"]["local"]["launcher_ready"])
        self.assertEqual(by["nes"]["local"]["launcher_url"], "https://www.retroarch.com/")
        self.assertTrue(by["nes"]["logo_url"].startswith("/api/platform-logo/"))

    def test_02b_emudeck_rescan(self):
        status, payload = self.call("/api/emudeck/rescan", "POST", {})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["detected"])
        self.assertTrue(payload["launchers"])
        self.assertEqual(payload["options"]["nes"]["launchers"], ["retroarch"])
        self.assertIn("mesen", payload["options"]["nes"]["cores"])
        self.assertEqual(payload["options"]["switch"]["launchers"], [])

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
        self.assertTrue(rom["mapping"]["launcher_ready"])
        self.assertEqual(rom["mapping"]["launcher_url"], "https://www.retroarch.com/")
        self.assertEqual(rom["mapping"]["bios_missing"], [])

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

    def test_08b_firmware_upload_and_delete(self):
        import base64
        FakeRomM.firmware_uploads = []
        FakeRomM.firmware_deleted = []
        content = base64.b64encode(b"BIOSDATA").decode()
        status, payload = self.call("/api/firmware/upload", "POST", {
            "platform_id": 13, "file_name": "bios.bin", "content": content,
        })
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(FakeRomM.firmware_uploads), 1)
        upload = FakeRomM.firmware_uploads[0]
        self.assertEqual(upload["query"].get("platform_id"), ["13"])
        self.assertIn(b"BIOSDATA", upload["raw"])
        self.assertIn(b'filename="bios.bin"', upload["raw"])
        status, payload = self.call("/api/firmware/900/delete", "POST", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(FakeRomM.firmware_deleted[-1], {"firmware": [900], "delete_from_fs": [900]})

    def test_08c_local_bios_list_and_delete(self):
        status, payload = self.call("/api/bios/local/13")
        self.assertEqual(status, 200, payload)
        names = [f["name"] for f in payload["files"]]
        self.assertIn("bios.bin", names)
        status, payload = self.call("/api/bios/local/delete", "POST", {"platform_id": 13, "name": "bios.bin"})
        self.assertEqual(status, 200, payload)
        self.assertNotIn("bios.bin", [f["name"] for f in payload["files"]])

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

    def test_14_uninstall_with_saves(self):
        saves = os.path.join(self.emu, "saves/nes")
        states = os.path.join(self.emu, "states/nes")
        os.makedirs(saves, exist_ok=True)
        os.makedirs(states, exist_ok=True)
        save_file = os.path.join(saves, "1942 (Japan, USA).srm")
        state_file = os.path.join(states, "1942 (Japan, USA).state")
        with open(save_file, "wb") as handle:
            handle.write(b"SAVE")
        with open(state_file, "wb") as handle:
            handle.write(b"STATE")
        status, payload = self.call("/api/installs/180/uninstall", "POST", {"remove_saves": True})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["removed"])
        self.assertEqual(payload["saves_removed"], 2)
        self.assertFalse(os.path.exists(os.path.join(self.emu, "roms/nes/1942 (Japan, USA).zip")))
        self.assertFalse(os.path.exists(save_file))
        self.assertFalse(os.path.exists(state_file))
        _, installs = self.call("/api/installs")
        self.assertFalse(any(i["rom_id"] == 180 for i in installs["installs"]))
        _, detail = self.call("/api/roms/180")
        self.assertIsNone(detail["rom"]["installed"])

    def test_15_uninstall_keeps_saves_when_unchecked(self):
        save_dir = os.path.join(self.emu, "saves/switch/Astral Chain")
        os.makedirs(save_dir, exist_ok=True)
        save_file = os.path.join(save_dir, "main.sav")
        with open(save_file, "wb") as handle:
            handle.write(b"SAVE")
        status, payload = self.call("/api/installs/181/uninstall", "POST", {"remove_saves": False})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["saves_removed"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.emu, "roms/switch/Astral Chain")))
        self.assertTrue(os.path.exists(save_file))


class SaveManagementTests(unittest.TestCase):
    """The saves panel: list versions, choose one to play, manage the slot."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rommarcade-saves-")
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeRomM)
        cls.fake_port = cls.fake.server_address[1]
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()

        os.environ["ROMARCADE_HOME"] = os.path.join(cls.tmp, "home")
        os.environ["HOME"] = os.path.join(cls.tmp, "user")
        os.makedirs(os.path.join(os.environ["HOME"], ".var/app/org.libretro.RetroArch/config/retroarch/cores"), exist_ok=True)
        open(os.path.join(os.environ["HOME"], ".var/app/org.libretro.RetroArch/config/retroarch/cores/mesen_libretro.so"), "w").close()

        cls.emu = os.path.join(cls.tmp, "Emulation")
        for sub in ("roms/nes", "saves/nes", "bios", "tools/launchers"):
            os.makedirs(os.path.join(cls.emu, sub), exist_ok=True)
        cls.save_dir = os.path.join(cls.emu, "saves/nes")
        cls.marker = os.path.join(cls.tmp, "launch-save.txt")
        cls.args_file = os.path.join(cls.tmp, "launcher-args.txt")
        launcher = os.path.join(cls.emu, "tools/launchers/retroarch.sh")
        with open(launcher, "w") as handle:
            handle.write(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$@\" > {cls.args_file}\n"
                f'cat "{cls.save_dir}/1942 (Japan, USA).srm" > "{cls.marker}" 2>/dev/null || echo missing > "{cls.marker}"\n'
            )
        os.chmod(launcher, 0o755)

        cls.store = ConfigStore()
        cls.store.add_server(f"http://127.0.0.1:{cls.fake_port}", {"mode": "token", "token": TOKEN})
        cls.store.update({"emudeck": {"root": cls.emu}})
        server_id = cls.store.active_server()["id"]
        cls.store.update({"save_sync": {"enabled": True, "play_gate": True,
                                        "devices": {server_id: {"mode": "explicit", "device_id": "dev-test"}}}})
        cls.app = App(cls.store)
        cls.httpd = Server(("127.0.0.1", 0), Handler, cls.app)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

        target = os.path.join(cls.emu, "roms/nes/1942 (Japan, USA).zip")
        with open(target, "wb") as handle:
            handle.write(b"NES-ROM-DATA")
        cls.app.state.record_install(ROM_NES, target, 12, "nes")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.downloads.shutdown()
        cls.fake.shutdown()
        cls.fake.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        os.environ.pop("ROMARCADE_HOME", None)

    def setUp(self):
        FakeRomM.reset_saves("download")
        for name in os.listdir(self.save_dir):
            path = os.path.join(self.save_dir, name)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.unlink(path)
        state_file = os.path.join(os.environ["ROMARCADE_HOME"], "save-sync.json")
        if os.path.exists(state_file):
            os.unlink(state_file)
        self.app.save_sync.state = SyncState()
        for path in (self.marker, self.args_file):
            if os.path.exists(path):
                os.unlink(path)

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def wait_for(self, path, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path):
                return True
            time.sleep(0.05)
        return False

    def wait_for_content(self, path, expected, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(path, "rb") as handle:
                    if handle.read() == expected:
                        return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    def test_01_saves_panel_lists_versions(self):
        status, payload = self.call("/api/roms/180/saves")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["supported"])
        self.assertEqual(payload["slot"], "autosave")
        self.assertEqual(payload["playing"], "")
        row = next(item for item in payload["server"] if item["id"] == 900)
        self.assertEqual(row["slot"], "autosave")
        self.assertTrue(row["current"])
        self.assertFalse(row["favorite"])
        self.assertEqual(payload["slots"][0]["slot"], "autosave")

    def test_02_restore_then_play_waits_for_the_save(self):
        status, payload = self.call("/api/roms/180/saves/restore", "POST", {"save_id": 900, "play": True})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["play"]["launched"], payload)
        local = os.path.join(self.save_dir, "1942 (Japan, USA).srm")
        with open(local, "rb") as handle:
            self.assertEqual(handle.read(), FakeRomM.save_bytes)
        self.assertTrue(self.wait_for_content(self.marker, FakeRomM.save_bytes),
                        "the emulator must start after the save is in place")
        self.assertEqual(payload["uploaded"], 900, "the chosen version is promoted in place")
        self.assertEqual(FakeRomM.uploaded, [], "an identical version is not stored again")
        self.assertTrue(any(query["slot"] == "autosave" and not query["overwrite"]
                            for query in FakeRomM.upload_queries))
        _, panel = self.call("/api/roms/180/saves")
        self.assertEqual(panel["playing"], "1942 (Japan, USA).srm")
        self.assertTrue(any(item["matches_local"] for item in panel["server"]))

    def test_03_conflict_stops_the_launch(self):
        with open(os.path.join(self.save_dir, "1942 (Japan, USA).srm"), "wb") as handle:
            handle.write(b"LOCAL-DIFFERENT")
        FakeRomM.reset_saves("conflict")
        status, payload = self.call("/api/play", "POST", {"rom_id": 180})
        self.assertEqual(status, 409, payload)
        self.assertTrue(payload["save_conflict"])
        time.sleep(0.4)
        self.assertFalse(os.path.exists(self.args_file), "the emulator must not start on a conflict")

    def test_04_upload_a_local_save(self):
        with open(os.path.join(self.save_dir, "1942 (Japan, USA).srm"), "wb") as handle:
            handle.write(b"LOCAL-SAVE")
        FakeRomM.reset_saves("upload")
        status, payload = self.call("/api/roms/180/saves/upload", "POST", {"name": "1942 (Japan, USA).srm"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["slot"], "autosave")
        self.assertEqual(FakeRomM.uploaded[-1]["data"], b"LOCAL-SAVE")
        self.assertEqual(FakeRomM.uploaded[-1]["row"]["slot"], "autosave")

    def test_05_choose_the_sync_slot(self):
        status, payload = self.call("/api/roms/180/saves/slot", "POST", {"slot": "before-boss"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["slot"], "before-boss")
        with open(os.path.join(self.save_dir, "1942 (Japan, USA).srm"), "wb") as handle:
            handle.write(b"LOCAL-SAVE")
        _, panel = self.call("/api/roms/180/saves")
        main = next(item for item in panel["local"] if item["main"])
        self.assertEqual(main["slot"], "before-boss")

    def test_07_repeated_use_and_play_adds_no_saves(self):
        FakeRomM.reset_saves("download")
        status, first = self.call("/api/roms/180/saves/restore", "POST", {"save_id": 900})
        self.assertEqual(status, 200, first)
        _, panel = self.call("/api/roms/180/saves")
        rows = len(panel["server"])
        self.assertEqual(FakeRomM.uploaded, [], "the version is promoted, not duplicated")
        status, second = self.call("/api/roms/180/saves/restore", "POST", {"save_id": 900})
        self.assertEqual(status, 200, second)
        _, panel = self.call("/api/roms/180/saves")
        self.assertEqual(len(panel["server"]), rows, "a second Use must not add a version")
        self.assertEqual(FakeRomM.uploaded, [])
        self.assertEqual([item["name"] for item in panel["local"]],
                         ["1942 (Japan, USA).srm"],
                         "the pre-restore backup is hidden from the save list")

    def test_08_after_play_skips_an_unchanged_save(self):
        status, payload = self.call("/api/roms/180/saves/restore", "POST", {"save_id": 900})
        self.assertEqual(status, 200, payload)
        rom = self.app.rom_detail(180)
        summary = self.app.save_sync.after_play(
            rom, rom["mapping"], self.emu, self.app.client(), self.app.active_server()
        )
        self.assertEqual(summary["uploaded"], 0)
        self.assertEqual(summary.get("skipped"), 1)
        self.assertEqual(FakeRomM.uploaded, [])

    def test_10_create_a_sync_slot_seeds_it(self):
        with open(os.path.join(self.save_dir, "1942 (Japan, USA).srm"), "wb") as handle:
            handle.write(b"LOCAL-SAVE")
        FakeRomM.reset_saves("upload")
        status, payload = self.call("/api/roms/180/saves/slots", "POST",
                                    {"slot": "before-boss", "seed": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["slot"], "before-boss")
        self.assertTrue(payload["created"]["seeded"])
        self.assertEqual(FakeRomM.uploaded[-1]["row"]["slot"], "before-boss")
        self.assertEqual(FakeRomM.uploaded[-1]["data"], b"LOCAL-SAVE")
        self.assertIn("before-boss", [slot["slot"] for slot in payload["slots"]])
        self.assertEqual([item["name"] for item in payload["local"]],
                         ["1942 (Japan, USA).srm"])

    def test_11_create_a_slot_without_a_local_save(self):
        FakeRomM.reset_saves("download")
        status, payload = self.call("/api/roms/180/saves/slots", "POST",
                                    {"slot": "no-local", "seed": True})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["slot"], "no-local")
        self.assertFalse(payload["created"]["seeded"])
        self.assertEqual(FakeRomM.uploaded, [])

    def test_12_reject_an_empty_slot_name(self):
        status, payload = self.call("/api/roms/180/saves/slots", "POST", {"slot": "   "})
        self.assertEqual(status, 400, payload)

    def test_09_local_scan_ignores_our_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("Game.srm", "Game.srm.pre-restore-20261126-110357",
                         "Game.srm.part", ".Game.srm.tmp"):
                with open(os.path.join(tmp, name), "wb") as handle:
                    handle.write(b"x")
            os.makedirs(os.path.join(tmp, ".rommarcade", "backups"))
            self.assertEqual([save.name for save in local_saves(tmp, "Game")], ["Game.srm"])

    def test_06_manage_flags_and_download(self):
        status, payload = self.call("/api/saves/900/favorite", "POST", {"is_favorite": True})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["save"]["is_favorite"])
        status, payload = self.call("/api/saves/900/rename", "POST", {"file_name": "before the boss.srm"})
        self.assertEqual(payload["save"]["file_name"], "before the boss.srm")
        status, payload = self.call("/api/saves/900/visibility", "POST", {"is_public": True})
        self.assertTrue(payload["save"]["is_public"])
        request = urllib.request.Request(self.base + "/api/saves/900/download")
        with urllib.request.urlopen(request, timeout=15) as response:
            self.assertEqual(response.read(), FakeRomM.save_bytes)
            self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        status, payload = self.call("/api/saves/900/delete", "POST", {})
        self.assertTrue(payload["deleted"])
        self.assertIn(900, FakeRomM.deleted)


class LibraryIntegrationTests(unittest.TestCase):
    """A laptop with a RomM server but no EmuDeck: mirror the server's layout."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rommarcade-library-")
        cls.previous_home = os.environ.get("HOME")
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeRomM)
        cls.fake_port = cls.fake.server_address[1]
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()

        os.environ["ROMARCADE_HOME"] = os.path.join(cls.tmp, "home")
        os.environ["HOME"] = os.path.join(cls.tmp, "user")
        os.makedirs(os.environ["HOME"], exist_ok=True)

        cls.store = ConfigStore()
        cls.store.add_server(f"http://127.0.0.1:{cls.fake_port}", {"mode": "token", "token": TOKEN})
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
        if cls.previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = cls.previous_home

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_01_preview_before_setup(self):
        status, payload = self.call("/api/library")
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["configured"])
        self.assertTrue(payload["suggestions"]["romm"].endswith("RomM"))
        rows = {row["platform"]: row for row in payload["platforms"]}
        self.assertIn("nes", rows)
        self.assertEqual(rows["nes"]["games"], 1)
        self.assertEqual(rows["nes"]["folder"], "nes")
        self.assertFalse(rows["nes"]["exists"])
        _, status_payload = self.call("/api/status")
        self.assertEqual(status_payload["emudeck"]["mode"], "")
        self.assertFalse(status_payload["emudeck"]["detected"])
        self.assertFalse(status_payload["emudeck"]["usable"])

    def test_02_create_mirror_layout(self):
        root = os.path.join(self.tmp, "RomM")
        status, payload = self.call("/api/library", "POST", {"mode": "romm", "root": root})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["library"]["configured"])
        self.assertEqual(payload["library"]["roms_root"], os.path.join(root, "roms"))
        self.assertEqual(payload["library"]["bios_root"], os.path.join(root, "bios"))
        self.assertEqual(sorted(payload["created"]), ["nes", "switch"])
        for sub in ("roms/nes", "roms/switch", "bios"):
            self.assertTrue(os.path.isdir(os.path.join(root, sub)), sub)

    def test_03_platforms_and_status_use_library(self):
        status, payload = self.call("/api/platforms")
        self.assertEqual(status, 200, payload)
        by = {p["fs_slug"]: p for p in payload["platforms"]}
        self.assertEqual(by["nes"]["local"]["dir"], "nes")
        self.assertTrue(by["nes"]["local"]["exists"])
        self.assertEqual(by["nes"]["local"]["launcher"], "")
        _, status_payload = self.call("/api/status")
        self.assertEqual(status_payload["emudeck"]["mode"], "library")
        self.assertTrue(status_payload["emudeck"]["usable"])
        self.assertFalse(status_payload["emudeck"]["detected"])
        self.assertEqual(
            status_payload["emudeck"]["library"]["roms_root"],
            os.path.join(self.tmp, "RomM", "roms"),
        )

    def test_04_install_works_without_emudeck(self):
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
        target = os.path.join(self.tmp, "RomM", "roms/nes/1942 (Japan, USA).zip")
        self.assertTrue(os.path.exists(target))
        status, payload = self.call("/api/play", "POST", {"rom_id": 180})
        self.assertEqual(status, 409)
        self.assertIn("launcher", payload["error"].lower())

    def test_05_clear_setup(self):
        status, payload = self.call("/api/library", "POST", {"mode": "later"})
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["library"]["configured"])
        # Clearing the choice must not delete folders or installed games.
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "RomM", "roms", "nes")))
        _, status_payload = self.call("/api/status")
        self.assertEqual(status_payload["emudeck"]["mode"], "")
        self.assertFalse(status_payload["emudeck"]["usable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
