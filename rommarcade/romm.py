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
    ):
        data = None
        request_headers = dict(self._headers)
        if body is not None:
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
