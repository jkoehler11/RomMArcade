"""Command line entry point: `rommarcade serve`, `open`, `status`."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from . import PRODUCT, PRODUCT_TAGLINE, __version__
from .config import ConfigStore, config_dir, data_dir, normalize_url
from .deeplink import parse_deeplink
from .server import run


def _local_base() -> str:
    cfg = ConfigStore().get()
    port = int(cfg.get("web", {}).get("port") or 8790)
    return f"http://127.0.0.1:{port}"


def _request_json(method: str, url: str, body: dict | None = None, timeout: float = 5.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode()) if raw else {}


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = ConfigStore()
    data = cfg.get()
    host = args.host or data.get("web", {}).get("bind") or "0.0.0.0"
    port = int(args.port or data.get("web", {}).get("port") or 8790)
    if args.open_url:
        try:
            target = parse_deeplink(args.open_url)
        except Exception as exc:  # noqa: BLE001
            print(f"{PRODUCT}: {exc}", file=sys.stderr)
            return 2
        # The server owns the queue file; pushing through the HTTP API after it
        # starts keeps one writer.
        args._target = target  # type: ignore[attr-defined]

    stop = threading.Event()

    def _stop(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if getattr(args, "_target", None):
        # Give the service a moment to bind, then hand the target to it.
        def _push():
            deadline = time.time() + 15
            while time.time() < deadline and not stop.is_set():
                try:
                    _request_json("POST", _local_base() + "/api/deeplink", {"url": args.open_url})
                    return
                except (urllib.error.URLError, OSError, json.JSONDecodeError):
                    time.sleep(0.25)

        threading.Thread(target=_push, daemon=True).start()

    from .server import App, Server, Handler

    app = App(cfg)
    httpd = Server((host, port), Handler, app)
    print(f"{PRODUCT} {__version__} — {PRODUCT_TAGLINE}")
    print(f"Listening on http://{host}:{port}  (config: {config_dir()})")
    app.log(f"{PRODUCT} {__version__} listening on http://{host}:{port}")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    while not stop.is_set():
        stop.wait(0.5)
    httpd.shutdown()
    app.downloads.shutdown()
    httpd.server_close()
    app.log(f"{PRODUCT} stopped")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    try:
        target = parse_deeplink(args.url)
    except Exception as exc:  # noqa: BLE001
        print(f"{PRODUCT}: {exc}", file=sys.stderr)
        return 2
    base = _local_base()
    try:
        _request_json("POST", base + "/api/deeplink", {"url": args.url})
        print(f"Handed to the running RomMArcade: rom {target['rom']}")
        return 0
    except (urllib.error.URLError, OSError):
        print(
            f"{PRODUCT} is not answering on {base}. Start it with `rommarcade serve` "
            "or `systemctl --user start rommarcade`.",
            file=sys.stderr,
        )
        return 2


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    base = _local_base()
    try:
        status = _request_json("GET", base + "/api/status")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"{PRODUCT} is not running ({exc}).")
        return 1
    server = status.get("server") or {}
    print(f"{status.get('product')} {status.get('version')}")
    if server.get("configured"):
        state = "reachable" if server.get("reachable") else "unreachable"
        print(f"Server: {server.get('url')} ({state}, {server.get('version') or 'unknown version'})")
        if server.get("user"):
            print(f"User:   {server['user'].get('username')} ({server['user'].get('role')})")
    else:
        print("Server: not configured yet")
    emu = status.get("emudeck") or {}
    print(f"EmuDeck: {'detected at ' + emu.get('root', '') if emu.get('detected') else 'not found'}")
    print(f"Active downloads: {status.get('downloads', 0)}")
    return 0


def cmd_where(args: argparse.Namespace) -> int:  # noqa: ARG001
    print(f"config: {config_dir()}")
    print(f"data:   {data_dir()}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:  # noqa: ARG001
    print(f"{PRODUCT} {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rommarcade", description=f"{PRODUCT} — {PRODUCT_TAGLINE}")
    parser.add_argument("--version", action="version", version=f"{PRODUCT} {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the local service and web UI")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--open-url", default="", help="queue a rommarcade:// URL at startup")
    serve.set_defaults(func=cmd_serve)

    open_cmd = sub.add_parser("open", help="hand a rommarcade:// URL to the running service")
    open_cmd.add_argument("url")
    open_cmd.set_defaults(func=cmd_open)

    status = sub.add_parser("status", help="print the running service's status")
    status.set_defaults(func=cmd_status)

    where = sub.add_parser("where", help="print config and data directories")
    where.set_defaults(func=cmd_where)

    version = sub.add_parser("version", help="print the version")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["serve"])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
