"""Native desktop shell for RomMArcade.

The app is the same local service and UI as `rommarcade serve`, but it runs in
a native webview window instead of a browser tab.  That keeps one codebase for
Linux, macOS and Windows: the OS supplies the webview, the Python side supplies
RomM and EmuDeck.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

from . import PRODUCT, PRODUCT_TAGLINE, __version__
from .config import ConfigStore, config_dir, data_dir
from .server import App, Handler, Server


def _port_file() -> str:
    return os.path.join(data_dir(), "desktop-port")


def _probe(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=1.5) as response:
            data = json.loads(response.read() or b"{}")
        return data.get("product") == PRODUCT
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return False


def _remember_port(port: int) -> None:
    try:
        os.makedirs(os.path.dirname(_port_file()) or ".", exist_ok=True)
        with open(_port_file(), "w", encoding="utf-8") as handle:
            handle.write(str(port))
    except OSError:
        pass


def _forget_port(port: int) -> None:
    try:
        with open(_port_file(), "r", encoding="utf-8") as handle:
            remembered = handle.read().strip()
        if remembered == str(port):
            os.unlink(_port_file())
    except OSError:
        pass


def _existing_base() -> str:
    candidates = ["http://127.0.0.1:8790"]
    try:
        with open(_port_file(), "r", encoding="utf-8") as handle:
            port = int(handle.read().strip() or 0)
        if port:
            candidates.insert(0, f"http://127.0.0.1:{port}")
    except (OSError, ValueError):
        pass
    for base in candidates:
        if _probe(base):
            return base
    return ""


def run(port: int = 0, debug: bool = False, reuse: bool = True) -> int:
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        print(
            f"{PRODUCT}: the desktop shell needs pywebview.\n"
            "Install it with: python3 -m pip install \"rommarcade[desktop]\"\n"
            "Linux also needs WebKitGTK and PyGObject from the distribution.",
            file=sys.stderr,
        )
        return 2

    existing = _existing_base() if reuse else ""
    if existing:
        print(f"{PRODUCT} {__version__} — opening {existing}")
        webview.create_window(
            PRODUCT,
            existing,
            width=1440,
            height=900,
            min_size=(1000, 640),
            confirm_close=False,
            text_select=True,
        )
        webview.start(debug=debug)
        return 0

    cfg = ConfigStore()
    data = cfg.get()
    host = "127.0.0.1"
    wanted = int(port or data.get("web", {}).get("port") or 8790)
    app = App(cfg)
    httpd: Server | None = None
    try:
        httpd = Server((host, wanted), Handler, app)
    except OSError:
        httpd = Server((host, 0), Handler, app)
    actual = int(httpd.server_address[1])
    base = f"http://{host}:{actual}"
    _remember_port(actual)

    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="rommarcade-http")
    thread.start()
    app.log(f"{PRODUCT} {__version__} desktop listening on {base}")
    print(f"{PRODUCT} {__version__} — {PRODUCT_TAGLINE}")
    print(f"Desktop window: {base}  (config: {config_dir()})")

    webview.create_window(
        PRODUCT,
        base,
        width=1440,
        height=900,
        min_size=(1000, 640),
        confirm_close=False,
        text_select=True,
    )
    try:
        webview.start(debug=debug)
    except Exception as exc:  # noqa: BLE001 - no display, broken webview, etc.
        print(f"{PRODUCT}: the desktop window could not open: {exc}", file=sys.stderr)
        return 2
    finally:
        httpd.shutdown()
        httpd.server_close()
        app.downloads.shutdown()
        _forget_port(actual)
        app.log(f"{PRODUCT} desktop closed")
    return 0
