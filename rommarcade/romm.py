"""A small, dependency-free client for the RomM HTTP API.

RomM (and the ROMulus fork) accepts Bearer client tokens and HTTP Basic
credentials on every API route.  Covers under /assets are public, but the
client never assumes that: the cover proxy uses the same auth headers.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterable

from . import USER_AGENT
from .config import auth_headers, normalize_url


class RomMError(Exception):
    """An error returned by a RomM server (or the network in front of it)."""

    def __init__(self, message: str, status: int = 0, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail

    @property
    def auth_failed(self) -> bool:
        return self.status in (401, 403)


class RomMClient:
    def __init__(
        self,
        base_url: str,
        auth: dict[str, Any] | None = None,
        tls_verify: bool = True,
        timeout: float = 25.0,
    ):
        self.base_url = normalize_url(base_url)
        self.auth = auth or {"mode": "none"}
        self.timeout = timeout
        self._headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        self._headers.update(auth_headers(self.auth))
        if tls_verify:
            self._context: ssl.SSLContext | None = None
        else:
            self._context = ssl.create_default_context()
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE

    # -- plumbing ---------------------------------------------------------
    def url(self, path: str, params: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        # RomM's asset paths carry a `?ts=2026-08-06 18:34:24` suffix with raw
        # spaces; keep the query structure, encode only what cannot be sent.
        path = urllib.parse.quote(path, safe="/:?&=%+@~!$,;'()*[]")
        url = self.base_url + path
        if params:
            encoded = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None},
                doseq=True,
            )
            if encoded:
                url += ("&" if "?" in url else "?") + encoded
        return url

    def _open(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
    ):
        request_headers = dict(self._headers)
        if data is not None:
            if content_type:
                request_headers["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            self.url(path, params), data=data, method=method, headers=request_headers
        )
        try:
            return urllib.request.urlopen(
                request, timeout=timeout or self.timeout, context=self._context
            )
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            message, status = _describe_http_error(exc, detail)
            raise RomMError(message, status, detail=detail) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise RomMError(f"No RomM server answered at {self.base_url} ({reason}).") from None
        except (TimeoutError, socket.timeout):
            raise RomMError(f"The RomM server at {self.base_url} timed out.") from None

    def json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        with self._open(method, path, params, body, timeout) as response:
            raw = response.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RomMError("The server answered with something that was not JSON.") from None

    def open(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ):
        """Open a streaming GET; the caller owns the response."""
        return self._open("GET", path, params, timeout=timeout)

    # -- endpoints --------------------------------------------------------
    def heartbeat(self) -> dict[str, Any]:
        return self.json("GET", "/api/heartbeat") or {}

    def me(self) -> dict[str, Any]:
        return self.json("GET", "/api/users/me") or {}

    def platforms(self) -> list[dict[str, Any]]:
        data = self.json("GET", "/api/platforms")
        return data if isinstance(data, list) else []

    def roms(
        self,
        platform_ids: Iterable[int] | None = None,
        search: str | None = None,
        limit: int = 60,
        offset: int = 0,
        order_by: str | None = None,
        order_dir: str = "asc",
        with_files: bool = False,
        rom_ids: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 500)),
            "offset": max(0, int(offset)),
            "with_total": "true",
        }
        ids = list(platform_ids or [])
        if ids:
            params["platform_ids"] = ids
        wanted = list(rom_ids or [])
        if wanted:
            params["rom_ids"] = wanted
        if search:
            params["search_term"] = search
        if order_by:
            params["order_by"] = order_by
            params["order_dir"] = order_dir
        if with_files:
            params["with_files"] = "true"
        data = self.json("GET", "/api/roms", params)
        if isinstance(data, list):  # very old builds
            return {"items": data, "total": len(data), "limit": params["limit"], "offset": params["offset"]}
        return data

    def rom(self, rom_id: int) -> dict[str, Any]:
        return self.json("GET", f"/api/roms/{int(rom_id)}") or {}

    def firmware(self, platform_id: int) -> list[dict[str, Any]]:
        data = self.json("GET", "/api/firmware", {"platform_id": int(platform_id)})
        if isinstance(data, dict):
            data = data.get("items", [])
        return data if isinstance(data, list) else []

    def upload_firmware(self, platform_id: int, file_name: str, content: bytes) -> dict[str, Any]:
        """Upload one BIOS file to RomM (multipart, replacing a same-named file)."""
        boundary = "RomMArcade" + uuid.uuid4().hex
        safe = str(file_name).replace('"', "_").replace("\r", "_").replace("\n", "_")
        # RomM declares platform_id as a plain int, so FastAPI reads it from the
        # query string; only the file belongs in the multipart body.
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{safe}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        with self._open(
            "POST",
            "/api/firmware",
            {"platform_id": int(platform_id)},
            data=head + content + tail,
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=180,
        ) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def delete_firmware(self, firmware_id: int) -> dict[str, Any]:
        return self.json(
            "POST",
            "/api/firmware/delete",
            body={"firmware": [int(firmware_id)], "delete_from_fs": [int(firmware_id)]},
        ) or {}

    def content_path(self, rom_id: int, file_name: str) -> str:
        quoted = urllib.parse.quote(str(file_name), safe="")
        return f"/api/roms/{int(rom_id)}/content/{quoted}"

    def firmware_path(self, firmware_id: int, file_name: str) -> str:
        quoted = urllib.parse.quote(str(file_name), safe="")
        return f"/api/firmware/{int(firmware_id)}/content/{quoted}"

    def open_content(self, rom_id: int, file_name: str, file_ids: Iterable[int] | None = None):
        params = None
        ids = list(file_ids or [])
        if ids:
            params = {"file_ids": ids}
        return self.open(self.content_path(rom_id, file_name), params, timeout=90)

    # -- saves and sync ---------------------------------------------------
    def register_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.json("POST", "/api/devices", body=payload) or {}

    def sync_negotiate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.json("POST", "/api/sync/negotiate", body=payload) or {}

    def open_save_content(
        self,
        save_id: int,
        device_id: str | None = None,
        optimistic: bool = True,
        session_id: int | None = None,
    ):
        """Open a save's bytes; the caller owns the response."""
        params: dict[str, Any] = {
            "device_id": device_id or None,
            "optimistic": "true" if optimistic else "false",
            "session_id": session_id,
        }
        return self.open(f"/api/saves/{int(save_id)}/content", params, timeout=120)

    def confirm_save_downloaded(
        self, save_id: int, device_id: str, content_hash: str | None = None
    ) -> dict[str, Any]:
        return self.json(
            "POST",
            f"/api/saves/{int(save_id)}/downloaded",
            body={"device_id": device_id, "content_hash": content_hash},
        ) or {}

    def upload_save(
        self,
        rom_id: int,
        data: bytes,
        *,
        file_name: str,
        emulator: str | None = None,
        slot: str | None = None,
        device_id: str | None = None,
        content_hash: str | None = None,
        overwrite: bool = False,
        session_id: int | None = None,
    ) -> dict[str, Any]:
        """POST one save as multipart/form-data (stdlib only)."""
        boundary = "----rommarcade" + uuid.uuid4().hex
        ascii_name = file_name.encode("ascii", "replace").decode("ascii").replace('"', "_")
        quoted = urllib.parse.quote(file_name)
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="saveFile"; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quoted}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii")
        body = head + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        params = {
            "rom_id": int(rom_id),
            "emulator": emulator or None,
            "slot": slot or None,
            "device_id": device_id or None,
            "content_hash": content_hash or None,
            "overwrite": "true" if overwrite else None,
            "session_id": session_id,
        }
        with self._open(
            "POST",
            "/api/saves",
            params,
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=120,
        ) as response:
            raw = response.read()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RomMError("The server answered a save upload with something that was not JSON.") from None

    def saves(self, rom_id: int | None = None, **params: Any) -> list[dict[str, Any]]:
        query: dict[str, Any] = dict(params)
        if rom_id is not None:
            query["rom_id"] = int(rom_id)
        data = self.json("GET", "/api/saves", query or None)
        if isinstance(data, dict):
            data = data.get("items") or data.get("saves") or []
        return data if isinstance(data, list) else []

    # -- save states ------------------------------------------------------
    def states(self, rom_id: int | None = None, **params: Any) -> list[dict[str, Any]]:
        """Every save state RomM holds for a ROM (or the whole account)."""
        query: dict[str, Any] = dict(params)
        if rom_id is not None:
            query["rom_id"] = int(rom_id)
        data = self.json("GET", "/api/states", query or None)
        if isinstance(data, dict):
            data = data.get("items") or data.get("states") or []
        return data if isinstance(data, list) else []

    def state(self, state_id: int) -> dict[str, Any]:
        return self.json("GET", f"/api/states/{int(state_id)}") or {}

    def open_state_content(self, state_id: int):
        """Open a save state's bytes; the caller owns the response."""
        return self.open(f"/api/states/{int(state_id)}/content", timeout=180)

    def upload_state(
        self,
        rom_id: int,
        data: bytes,
        *,
        file_name: str,
        emulator: str | None = None,
        core: str | None = None,
    ) -> dict[str, Any]:
        """POST one save state as multipart/form-data (stdlib only)."""
        boundary = "----rommarcade" + uuid.uuid4().hex
        ascii_name = file_name.encode("ascii", "replace").decode("ascii").replace('"', "_")
        quoted = urllib.parse.quote(file_name)
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="stateFile"; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quoted}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii")
        body = head + data + f"\r\n--{boundary}--\r\n".encode("ascii")
        params = {
            "rom_id": int(rom_id),
            "emulator": emulator or None,
            "core": core or None,
        }
        with self._open(
            "POST",
            "/api/states",
            params,
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout=180,
        ) as response:
            raw = response.read()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RomMError("The server answered a save-state upload with something that was not JSON.") from None

    def save(self, save_id: int) -> dict[str, Any]:
        return self.json("GET", f"/api/saves/{int(save_id)}") or {}

    def delete_saves(self, save_ids: Iterable[int]) -> Any:
        return self.json("POST", "/api/saves/delete", body={"saves": [int(i) for i in save_ids]})

    def update_save_favorite(self, save_id: int, is_favorite: bool) -> dict[str, Any]:
        return self.json(
            "PUT", f"/api/saves/{int(save_id)}/favorite", body={"is_favorite": bool(is_favorite)}
        ) or {}

    def update_save_labels(self, save_id: int, labels: Iterable[str]) -> dict[str, Any]:
        return self.json(
            "PUT", f"/api/saves/{int(save_id)}/labels", body={"labels": list(labels)}
        ) or {}

    def update_save_visibility(self, save_id: int, is_public: bool) -> dict[str, Any]:
        return self.json(
            "PUT", f"/api/saves/{int(save_id)}/visibility", body={"is_public": bool(is_public)}
        ) or {}

    def rename_save(self, save_id: int, file_name: str) -> dict[str, Any]:
        return self.json(
            "PUT", f"/api/saves/{int(save_id)}/file-name", body={"file_name": str(file_name)}
        ) or {}

    # -- convenience ------------------------------------------------------
    def probe(self) -> dict[str, Any]:
        heartbeat = self.heartbeat()
        system = heartbeat.get("SYSTEM") or {}
        out: dict[str, Any] = {
            "url": self.base_url,
            "version": system.get("VERSION") or system.get("VERSION_NUMBER") or "",
            "system": system,
            "user": None,
            "platforms": None,
        }
        if self.auth.get("mode") != "none":
            me = self.me()
            out["user"] = {
                "id": me.get("id"),
                "username": me.get("username"),
                "role": me.get("role"),
            }
            out["platforms"] = len(self.platforms())
        return out


def _describe_http_error(exc: urllib.error.HTTPError, detail: Any = None) -> tuple[str, int]:
    message = ""
    if isinstance(detail, dict):
        message = str(detail.get("detail") or detail.get("message") or "")
    elif isinstance(detail, str):
        message = detail
    if not message:
        message = exc.reason if isinstance(exc.reason, str) else f"HTTP {exc.code}"
    if exc.code == 401:
        message = "The RomM server refused those credentials."
    elif exc.code == 403:
        message = "This RomM account is not allowed to do that."
    elif exc.code == 404:
        message = "The RomM server has no such item."
    return message, int(exc.code)


def _error_detail(exc: urllib.error.HTTPError) -> Any:
    try:
        raw = exc.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def content_filename(response, fallback: str) -> str:
    """Pick the server's filename out of Content-Disposition when present."""
    header = ""
    try:
        header = response.headers.get("Content-Disposition", "") or ""
    except Exception:
        pass
    marker = "filename*=UTF-8''"
    if marker in header:
        value = header.split(marker, 1)[1].split(";")[0].strip().strip('"')
        if value:
            return urllib.parse.unquote(value)
    if "filename=" in header:
        value = header.split("filename=", 1)[1].split(";")[0].strip().strip('"')
        if value:
            return urllib.parse.unquote(value)
    return fallback
