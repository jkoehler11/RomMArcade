# Development

RomMArcade is a Python service with a vanilla JavaScript UI. There is no frontend build step and
no database.

## Build from source

```bash
git clone https://github.com/jkoehler11/RomMArcade.git
cd RomMArcade
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[build]"
python packaging/build_desktop.py
```

Linux also needs WebKitGTK and PyGObject from the distribution, for example on Arch:

```bash
sudo pacman -S webkit2gtk-4.1 python-gobject
```

Run the app:

```bash
# from the build
./dist/RomMArcade/RomMArcade

# or from a source checkout with the desktop extra
rommarcade app
```

## Tests

```bash
python3 -m unittest tests.test_integration -v
```

The integration suite uses a fake RomM server, a throwaway EmuDeck tree, and a laptop-style
game-folder setup. It covers pairing, browsing, the games-only filter, cover caching, installs
(single-file and multi-file zip), the mirrored RomM folder layout, BIOS, launching, save sync and
the deep-link queue end to end. It needs no network and currently runs 63 tests.

For UI work, run a local service and point a browser at it:

```bash
ROMARCADE_HOME=/tmp/rommarcade-dev python3 -m rommarcade serve --port 8791
```

## Layout

```text
rommarcade/
├── config.py       # URLs, server profiles, secrets, atomic config
├── romm.py         # RomM HTTP API client (Bearer/Basic, streams)
├── emudeck.py      # discovery, platform map, launchers, cores, spawn
├── downloads.py    # threaded queue, atomic writes, safe zip extraction
├── state.py        # installs and play history
├── deeplink.py     # rommarcade:// parsing and the consume-once queue
├── server.py       # local JSON API + static UI
├── desktop.py      # native webview window and local service lifecycle
└── web/            # index.html, app.css, app.js (no framework, no build)

packaging/
├── build_desktop.py  # PyInstaller build for the current OS
├── RomMArcade.spec   # bundle recipe (app, .app, .exe)
└── install_desktop.sh
```

## Design notes

- A server is a URL. RomM addresses are configurable and can include a sub-path or `/api`.
- EmuDeck is the source of truth for emulators, launch scripts, cores and ROM folders.
- Installs and downloads are atomic. A download that is not the expected file is rejected, not
  installed.
- API responses are `no-store`; a stale status panel would otherwise lie about the filesystem.
- Save sync resolves the emulator's real save folder, not the per-platform sibling that looks
  tempting.
