# RomMArcade

**Your RomM library, in an arcade shell.**

RomMArcade is a new, self-hosted front-end for [RomM](https://github.com/rommapp/romm) (and its
ROMulus fork) built around two ideas: a server is a **URL**, and EmuDeck already knows how to play.
It browses your RomM library, installs games into EmuDeck's own ROM folders, downloads BIOS, and
starts EmuDeck's launchers — no second emulator configuration to keep in sync.

It is a local service plus a web UI, written against the Python standard library and vanilla
JavaScript. There is no package manager step, no build step, and no database.

```
RomM server  ──HTTP──▶  RomMArcade  ──files──▶  ~/Emulation/roms/<system>
   (URL)                (local UI)   ──exec───▶  Emulation/tools/launchers/<emu>.sh
```

## What it does

- **Server URLs, not baked-in hosts.** Keep several RomM addresses and switch between them. Any
  `http(s)://host:port`, a bare `host:port`, or a server behind a sub-path works.
- **Pairing, tokens or passwords.** RomM's device flow gives this machine its own revocable token;
  a pasted client token or HTTP Basic credentials are also supported.
- **A library built for a couch.** Platform rails, cover grids, search, install state, sizes, and
  a game page that says exactly where the ROM will land and what will open it.
- **EmuDeck-native installs.** ROMs go to the EmuDeck folder for their platform. Multi-file ROMs
  arrive as a folder. BIOS downloads keep RomM's `bios/…` layout inside `~/Emulation/bios`.
- **EmuDeck-native launches.** RomMArcade runs the same launcher scripts EmuDeck writes
  (`retroarch.sh`, `dolphin-emu.sh`, `duckstation.sh`, `pcsx2-qt.sh`, `melonds.sh`, `azahar.sh`,
  `cemu.sh`, `flycast.sh`, …), passing a RetroArch core when the system needs one.
- **A URL scheme.** `rommarcade://open?rom=180&play=1` opens, installs and starts a game from a
  browser, a desktop shortcut, or RomM's own **Play** menu through the bundled plugin manifest.
- **Live install progress.** A download tray with per-job progress, speed, cancel, and automatic
  play-after-install when the link asked for it.

## Install

Requirements: Linux with Python 3.10+ and an existing [EmuDeck](https://www.emudeck.com/) install
for actually playing games. RomM 5.x or ROMulus is the server side.

```bash
git clone https://github.com/jkoehler11/RomMArcade.git
cd RomMArcade
./packaging/install.sh
```

The installer copies the app to `~/.local/share/rommarcade`, puts `rommarcade`,
`rommarcade-open` and `rommarcade-launch` in `~/.local/bin`, installs the URL handler, and enables
a `systemd --user` service. Nothing is installed system-wide and nothing needs root.

Then open <http://127.0.0.1:8790> and follow the setup wizard:

1. Enter the RomM address, for example `http://192.168.86.30:7676`.
2. Pair the device (RomM shows a short code), or paste a client token / use a password.
3. Confirm the detected EmuDeck root and enter the arcade.

```bash
rommarcade status      # is the service up, is RomM reachable
rommarcade where       # config and data directories
rommarcade open 'rommarcade://open?rom=180'
```

Config lives in `~/.config/rommarcade/config.json` (mode 600). Data — installs, covers, logs — lives
in `~/.local/share/rommarcade`.

## URL support

### Server URLs

The setup wizard and **Settings → Server** accept:

```
192.168.86.30:7676                 → http://192.168.86.30:7676
http://romm.lan:7676/              → http://romm.lan:7676
https://romm.example.com/romm/     → https://romm.example.com/romm
http://host:7676/api               → http://host:7676
```

A trailing `/api` is stripped, so the address you paste from RomM's API docs works too. Local and
private hosts default to `http://`; public hostnames default to `https://`.

### Deep links

```
rommarcade://open?rom=180
rommarcade://open?rom=180&platform=nes&name=1942
rommarcade://open?rom=180&play=1
rommarcade://play?rom=180
```

`play=1` (or the `play` action) installs the game first if it is missing, then launches it. A target
is consumed once: reloading the page does not navigate again.

`packaging/install.sh` registers `x-scheme-handler/rommarcade`; `rommarcade-open` hands the URL to
the running service and starts it if necessary.

### The RomM plugin

`plugin/plugin.json` is a manifest for RomM's plugin shelf. It adds **Play in RomMArcade** to a
game's Play menu, using `rommarcade://open?rom={rom_id}&platform={platform_slug}&name={rom_name}`.
Install it in RomM under **Settings → Plugins**, or point the server at the raw file:

```
https://raw.githubusercontent.com/jkoehler11/RomMArcade/main/plugin/plugin.json
```

Its two settings: hide the button, and append `&play=1` so the button installs and starts the game
in one press. The plugin is optional; the app works without it.

## EmuDeck integration

RomMArcade reads `~/.config/EmuDeck/settings.json` for `storagePath` and expects the usual tree:

```
~/Emulation/
├── roms/<system>/          # installs land here
├── bios/                   # RomM firmware downloads land here
└── tools/launchers/*.sh    # the launchers RomMArcade starts
```

Platform folders are matched on RomM's `fs_slug` first, then by known aliases (`ngc → gc`,
`3ds → n3ds`, `dc → dreamcast`, `sega32 → sega32x`, `ps1 → psx`, …), then by a normalized name.
Every mapping is visible and editable in **Settings → Emulators**: folder, launcher, and libretro
core per platform. Overrides live in the config, not in EmuDeck.

Launcher choice follows the system: standalone emulators where EmuDeck installs them (Dolphin,
Cemu, DuckStation, PCSX2, PPSSPP, melonDS, Azahar, Flycast, Xenia), RetroArch otherwise. For
RetroArch, RomMArcade picks the first core that actually exists in the installed core folder
(`mesen` for NES, `snes9x` for SNES, `mgba` for GBA, `mupen64plus_next` for N64, `fbneo` for
arcade, …) and falls back to the launcher's own default if none is found.

Launches are detached from the service and inherit the graphical session (`XDG_RUNTIME_DIR`,
`WAYLAND_DISPLAY` by socket discovery), so closing the browser does not close the game.

**What RomMArcade does not do (yet):** save/state sync, Steam shortcut creation, ROM patching,
and multi-disc conversion. Those remain EmuDeck/RomM's business; this app deliberately keeps its
hands off files it did not create.

## Development

```bash
python3 -m unittest tests.test_integration -v   # 17 tests, no network
ROMARCADE_HOME=/tmp/rommarcade-dev python3 -m rommarcade serve --port 8791
```

The test suite starts a fake RomM server and a throwaway EmuDeck tree, then exercises pairing,
browsing, cover caching, installs (single-file and multi-file zip), BIOS, launching, and the
deep-link queue end to end.

Layout:

```
rommarcade/
├── config.py       # URLs, server profiles, secrets, atomic config
├── romm.py         # RomM HTTP API client (Bearer/Basic, streams)
├── emudeck.py      # discovery, platform map, launchers, cores, spawn
├── downloads.py    # threaded queue, atomic writes, safe zip extraction
├── state.py        # installs and play history
├── deeplink.py     # rommarcade:// parsing and the consume-once queue
├── server.py       # local JSON API + static UI
└── web/            # index.html, app.css, app.js (no framework, no build)
```

## License

MIT. RomMArcade is an independent client; RomM, ROMulus and EmuDeck remain their authors' work.
