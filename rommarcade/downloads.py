"""Download queue for ROMs and BIOS files.

Stdlib only: a small thread pool, atomic `.part` writes and a safe zip
extractor.  Progress is polled by the UI; the service never holds the HTTP
response of a download open for the browser.
"""

from __future__ import annotations

import copy
import os
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .emudeck import EmuDeckError, bios_target, install_path, rom_file, sanitize_name
from .state import StateStore


def _now() -> float:
    return time.time()


class DownloadManager:
    def __init__(self, state: StateStore, log_file: str, concurrency: int = 2):
        self._state = state
        self._log_file = log_file
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(concurrency or 2)), thread_name_prefix="rommarcade-dl")
        self._closed = False

    # -- public -----------------------------------------------------------
    def enqueue_rom(self, rom: dict[str, Any], mapping: dict[str, Any], client) -> dict[str, Any]:
        target = install_path(rom, mapping)
        size = int(rom.get("fs_size_bytes") or 0)
        existing = self._state.install_for(int(rom.get("id")))
        if existing and os.path.exists(existing["path"]):
            return {
                "id": "already-" + str(rom.get("id")),
                "kind": "rom",
                "rom_id": int(rom.get("id")),
                "name": rom.get("name") or "",
                "platform": mapping.get("platform") or "",
                "dest": existing["path"],
                "total": int(existing.get("size") or size),
                "received": int(existing.get("size") or size),
                "status": "done",
                "error": "",
                "already": True,
                "started_at": _now(),
                "finished_at": _now(),
            }
        if size:
            free = shutil.disk_usage(os.path.dirname(target) or ".").free
            if free < size + 64 * 1024 * 1024:
                raise EmuDeckError(
                    f"Not enough free space for {rom.get('name') or 'this game'} "
                    f"({size / 1e9:.1f} GB needed)."
                )
        job = self._new_job(
            kind="rom",
            name=rom.get("name") or "",
            platform=mapping.get("platform") or "",
            dest=target,
            total=size,
            rom_id=int(rom.get("id")),
        )
        job["payload"] = {"rom": rom, "mapping": mapping}
        self._submit(job, client)
        return self._public(job)

    def enqueue_bios(self, platform: dict[str, Any], firmware: list[dict[str, Any]], bios_root: str, client) -> list[dict[str, Any]]:
        if not bios_root:
            raise EmuDeckError("No BIOS folder is set on this machine. Choose where games should be stored first.")
        created = []
        for item in firmware:
            target = bios_target(item, bios_root)
            if os.path.exists(target) and os.path.getsize(target) == int(item.get("file_size_bytes") or 0):
                continue
            job = self._new_job(
                kind="bios",
                name=item.get("file_name") or "firmware",
                platform=str(platform.get("fs_slug") or platform.get("slug") or ""),
                dest=target,
                total=int(item.get("file_size_bytes") or 0),
                firmware_id=int(item.get("id")),
            )
            job["payload"] = {"firmware": item}
            self._submit(job, client)
            created.append(self._public(job))
        return created

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._public(job) for job in self._jobs.values()]
        jobs.sort(key=lambda j: j.get("started_at") or 0, reverse=True)
        return jobs[:50]

    def active(self) -> bool:
        with self._lock:
            return any(j["status"] in ("queued", "downloading", "extracting") for j in self._jobs.values())

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            event = self._cancels.get(job_id)
            if not job:
                return None
            if event:
                event.set()
            if job["status"] == "queued":
                job["status"] = "canceled"
                job["finished_at"] = _now()
            return self._public(job)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for event in self._cancels.values():
                event.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- internals --------------------------------------------------------
    def _new_job(self, *, kind: str, name: str, platform: str, dest: str, total: int, **extra) -> dict[str, Any]:
        job = {
            "id": "j-" + uuid.uuid4().hex[:10],
            "kind": kind,
            "name": name,
            "platform": platform,
            "dest": dest,
            "total": int(total or 0),
            "received": 0,
            "status": "queued",
            "error": "",
            "started_at": _now(),
            "finished_at": None,
            "speed": 0.0,
        }
        job.update(extra)
        with self._lock:
            self._jobs[job["id"]] = job
            self._cancels[job["id"]] = threading.Event()
        return job

    def _submit(self, job: dict[str, Any], client) -> None:
        cancel = self._cancels[job["id"]]
        self._pool.submit(self._run, job, client, cancel)

    def _update(self, job: dict[str, Any], **fields) -> None:
        with self._lock:
            job.update(fields)

    def _run(self, job: dict[str, Any], client, cancel: threading.Event) -> None:
        payload = job.pop("payload", {})
        try:
            if cancel.is_set():
                raise _Canceled()
            self._update(job, status="downloading")
            if job["kind"] == "rom":
                self._download_rom(job, payload["rom"], payload["mapping"], client, cancel)
            else:
                self._download_firmware(job, payload["firmware"], client, cancel)
        except _Canceled:
            self._update(job, status="canceled", finished_at=_now())
            self._cleanup_partials(job)
        except Exception as exc:  # noqa: BLE001 - a job failure must stay a job failure
            self._update(job, status="error", error=str(exc), finished_at=_now())
            self._log(f"job {job['id']} failed: {exc}")
        finally:
            self._update(job, speed=0.0)

    def _download_rom(self, job: dict[str, Any], rom: dict[str, Any], mapping: dict[str, Any], client, cancel: threading.Event) -> None:
        dest = job["dest"]
        parent = os.path.dirname(dest) or "."
        os.makedirs(parent, exist_ok=True)
        file_info = rom_file(rom) or {}
        file_name = file_info.get("file_name") or rom.get("fs_name") or rom.get("file_name")
        if not file_name:
            raise EmuDeckError("The server did not name a file for this game.")
        response = client.open_content(int(rom["id"]), file_name)
        try:
            content_type = (response.headers.get("Content-Type") or "").lower()
            is_zip = "zip" in content_type or file_name.lower().endswith(".zip")
            if rom.get("has_multiple_files") and is_zip:
                self._download_zip(job, response, rom, dest, cancel)
            else:
                self._download_file(job, response, dest, cancel)
        finally:
            response.close()
        size = int(rom.get("fs_size_bytes") or job.get("received") or 0)
        self._state.record_install(rom, dest, size, mapping.get("platform") or "")
        self._update(job, status="done", finished_at=_now(), received=job.get("total") or job.get("received") or size)
        self._log(f"installed rom {rom.get('id')} {rom.get('name')!r} -> {dest}")

    def _download_firmware(self, job: dict[str, Any], firmware: dict[str, Any], client, cancel: threading.Event) -> None:
        dest = job["dest"]
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        response = client.open(client.firmware_path(int(firmware["id"]), firmware.get("file_name") or "firmware.bin"))
        try:
            self._download_file(job, response, dest, cancel)
        finally:
            response.close()
        self._update(job, status="done", finished_at=_now(), received=job.get("total") or job.get("received") or 0)
        self._log(f"bios {firmware.get('file_name')} -> {dest}")

    def _download_file(self, job: dict[str, Any], response, dest: str, cancel: threading.Event) -> None:
        total = job.get("total") or int(response.headers.get("Content-Length") or 0)
        self._update(job, total=total)
        part = dest + ".part"
        received = 0
        started = _now()
        last_emit = started
        with open(part, "wb") as handle:
            while True:
                if cancel.is_set():
                    raise _Canceled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                now = _now()
                speed = received / max(0.001, now - started)
                if now - last_emit > 0.15:
                    self._update(job, received=received, speed=speed)
                    last_emit = now
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, dest)
        self._update(job, received=received, speed=0.0)

    def _download_zip(self, job: dict[str, Any], response, rom: dict[str, Any], dest: str, cancel: threading.Event) -> None:
        archive = dest + ".download.zip"
        self._download_file(job, response, archive, cancel)
        self._update(job, status="extracting")
        staging = dest + ".extracting"
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if member.is_dir():
                        continue
                    name = member.filename.replace("\\", "/")
                    parts = [p for p in name.split("/") if p not in ("", ".")]
                    if name.startswith("/") or any(p == ".." for p in parts):
                        raise EmuDeckError("The server sent an archive with an unsafe path.")
                    target = os.path.join(staging, *parts)
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    with bundle.open(member) as source, open(target, "wb") as out:
                        shutil.copyfileobj(source, out)
        finally:
            try:
                os.unlink(archive)
            except OSError:
                pass
        os.makedirs(dest, exist_ok=True)
        for entry in os.listdir(staging):
            source = os.path.join(staging, entry)
            target = os.path.join(dest, entry)
            if os.path.isdir(source):
                if os.path.isdir(target):
                    shutil.rmtree(target)
                shutil.move(source, target)
            else:
                os.replace(source, target)
        shutil.rmtree(staging, ignore_errors=True)

    def _cleanup_partials(self, job: dict[str, Any]) -> None:
        for suffix in (".part", ".download.zip", ".extracting"):
            path = job.get("dest", "") + suffix
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            out = {k: copy.deepcopy(v) for k, v in job.items() if k != "payload"}
        out["progress"] = (
            min(1.0, out["received"] / out["total"]) if out.get("total") else (1.0 if out["status"] == "done" else 0.0)
        )
        return out

    def _log(self, message: str) -> None:
        try:
            os.makedirs(os.path.dirname(self._log_file) or ".", exist_ok=True)
            with open(self._log_file, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass


class _Canceled(Exception):
    pass
