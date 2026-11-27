# RomMArcade

**Your RomM library, in an arcade shell.**

RomMArcade is a desktop app for [RomM](https://github.com/rommapp/romm) (and its ROMulus fork)
built around two ideas: a server is a **URL**, and EmuDeck already knows how to play. It browses
your RomM library, installs games into EmuDeck's own ROM folders, downloads BIOS, and starts
EmuDeck's launchers — no second emulator configuration to keep in sync. On a machine without
EmuDeck it can still create a game library that mirrors the server's own folder layout, and hands
launching back to EmuDeck the moment it appears.

It runs as a native window on Linux, macOS and Windows, with the same Python service and vanilla
JavaScript UI behind it. There is no database and no browser tab; the window is the app.

```
RomM server  ──HTTP──▶  RomMArcade  ──files──▶  ~/Emulation/roms/<system>
   (URL)               (desktop app)  ──exec───▶  Emulation/tools/launchers/<emu>.sh
```

## What it does

- **Server URLs, not baked-in hosts.** Keep several RomM addresses and switch between them. Any
  `http(s)://host:port`, a bare `host:port`, or a server behind a sub-path works.
- **Pairing, tokens or passwords.** RomM's device flow gives this machine its own revocable token;
  a pasted client token or HTTP Basic credentials are also supported.
- **A library built for a couch.** Platform rails, cover grids, search, install state, sizes, and
  a game page that says exactly where the ROM will land and what will open it.
- **Games only, by design.** RomM may index emulator scripts, shader caches, patches and licence
  files that sit beside a ROM. RomMArcade filters those out and shows only entries with a real
  game file, so the shelf is the games — not the emulator payload.
- **EmuDeck installs the emulators.** RomMArcade never installs or updates emulators itself. It
  detects EmuDeck, maps each system to a launcher, and opens EmuDeck when something is missing.
  If a launcher has no EmuDeck installer behind it, the game page links to the emulator's official
  site instead. The integration can be switched off in **Settings → EmuDeck**; with it off RomMArcade is a
  browse-only RomM library.
- **EmuDeck-native installs.** ROMs go to the EmuDeck folder for their platform. Multi-file ROMs
  arrive as a folder. BIOS downloads keep RomM's `bios/…` layout inside `~/Emulation/bios`.
- **Machines without EmuDeck still get a library.** If there is no EmuDeck tree on this computer,
  the setup asks where games should live: mirror the RomM server's own layout
  (`<root>/roms/<system>` and `<root>/bios`) or create one in a folder of your choice, with a
  **Set up later** option that keeps the app browse-only until you decide.
- **EmuDeck-native launches.** RomMArcade runs the same launcher scripts EmuDeck writes
  (`retroarch.sh`, `dolphin-emu.sh`, `duckstation.sh`, `pcsx2-qt.sh`, `melonds.sh`, `azahar.sh`,
  `cemu.sh`, `flycast.sh`, …), passing a RetroArch core when the system needs one.
- **A URL scheme.** `rommarcade://open?rom=180&play=1` opens, installs and starts a game from a
  browser, a desktop shortcut, or RomM's own **Play** menu through the
  [RomMArcade plugin](https://github.com/jkoehler11/rommarcade-plugin).
- **Uninstall with a choice.** Remove an installed game from EmuDeck's ROM folder, with a checkbox
  to also remove that game's saves and save states from EmuDeck's `saves/` and `states/` folders.
- **Saves that arrive before the game does.** RomMArcade keeps a game's save in step with RomM
  around every launch: it pulls the current save before starting the emulator and pushes the save
  back when the emulator closes. A save that changed here and on the server stops the launch and
  says so instead of picking a winner.
- **Saves you can manage, like RomM.** The game page lists every save on the server and every
  save on this machine. Pick any version and **Use & play** starts the game from it (and makes it
  the save this machine syncs); favorite, label, rename, make public, download or delete a saved
  version; upload a local save to RomM; and choose which slot this game syncs on.
- **Live install progress.** A download tray with per-job progress, speed, cancel, and automatic
  play-after-install when the link asked for it. The install button itself becomes a disabled
  `Installing 42%` button while the job runs, and the separate install-only action disappears.

## Install

Requirements: Linux, macOS or Windows; Python 3.10+ only if building from source. EmuDeck is
needed for launching games; without it RomMArcade can still install into a game library that
mirrors your RomM server. RomM 5.x or ROMulus is the server side.

Prebuilt installers are produced by the `desktop` GitHub Actions workflow and published on the
[Releases](../../releases) page. Download the installer for your platform:

- **Linux:** run the `.AppImage` (after `chmod +x`). It needs WebKitGTK 4.1 and PyGObject from the
  distribution, for example `sudo pacman -S webkit2gtk-4.1 python-gobject` on Arch.
- **macOS:** open the `.pkg` installer. The build is unsigned, so use **Open** from the context menu
  the first time.
- **Windows:** run the `-setup.exe` installer. The build is unsigned, so SmartScreen may warn.

To build from source on any platform:

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

The setup wizard is the same: enter the RomM address, pair or paste a token, then either confirm
the EmuDeck it found or choose where games should live. You are in either way.

Config and data are per installation:

- Linux: `~/.config/rommarcade` and `~/.local/share/rommarcade`
- macOS: `~/Library/Application Support/RomMArcade`
- Windows: `%APPDATA%\RomMArcade` and `%LOCALAPPDATA%\RomMArcade`

### One app, one machine

Because this is a desktop app, each computer runs its own copy and uses its own EmuDeck, ROM
folders and launchers. There is no shared browser profile and no remote service to point at. Two
desktops can browse the same RomM library while installing and playing against their own local
emulators.

The older headless mode is still available for a Steam Deck, NAS or remote browser setup:

```bash
rommarcade serve --host 0.0.0.0 --port 8790
```

That mode is a local web service; it does not execute anything on the computer viewing the page.

## URL support

### Server URLs

The setup wizard and **Settings → Server** accept:

```
romm.local:7676                    → http://romm.local:7676
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

The plugin that adds **Play in RomMArcade** to RomM's Play menu lives in its own repository,
[jkoehler11/rommarcade-plugin](https://github.com/jkoehler11/rommarcade-plugin), together with the
`rommarcade://` handler for desktop installs that did not register one. Install it in RomM under
**Settings → Plugins**, or point the server at the raw manifest:

```
https://raw.githubusercontent.com/jkoehler11/rommarcade-plugin/main/plugin.json
```

That repository also documents its settings (`&play=1` makes the button install and start a game in
one press, clearing it only opens the game) and the platforms an action can be limited to. Nothing
in it runs on the RomM server, and the app works without it.

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
Every mapping is visible and editable in **Settings → EmuDeck**: folder, launcher, and libretro
core per platform. Overrides live in the config, not in EmuDeck.

**The on/off switch.** **Settings → EmuDeck** (or the EmuDeck page) toggles the whole integration.
With it off, install, play, BIOS, and launcher actions are refused by the API; the library remains
browsable. The default is on.

Launcher choice follows the system: standalone emulators where EmuDeck installs them (Dolphin,
Cemu, DuckStation, PCSX2, PPSSPP, melonDS, Azahar, Flycast, Xenia), RetroArch otherwise. For
RetroArch, RomMArcade picks the first core that actually exists in the installed core folder
(`mesen` for NES, `snes9x` for SNES, `mgba` for GBA, `mupen64plus_next` for N64, `fbneo` for
arcade, …) and falls back to the launcher's own default if none is found.

Launches are detached from the service and inherit the graphical session (`XDG_RUNTIME_DIR`,
`WAYLAND_DISPLAY` by socket discovery), so closing the browser does not close the game.

**Machines without EmuDeck.** A laptop that never ran EmuDeck has no `Emulation` tree, so the EmuDeck
step of the wizard becomes a game-folder step instead. It offers two layouts and a way to defer:

- **Same as my RomM server** — mirrors the server's own library layout under a root you pick,
  usually `~/RomM`: `roms/<fs_slug>/` per platform and `bios/`. The platform folder names come from
  the server, so the local library reads exactly like the server's.
- **My own folder** — one folder per platform directly inside a folder you choose (`~/ROMs/nes`,
  `~/ROMs/switch`, …) with a `bios/` sibling.
- **Set up later** — finish onboarding browse-only. The library page keeps a *No game folders yet*
  strip, and the EmuDeck page offers the same chooser whenever you want to come back.

Only platforms that have games on the server get a folder, and existing folders win: a collection
that already uses EmuDeck names (`psx` for `ps1`, `gc` for `ngc`) is reused rather than duplicated.
Installs, uninstall and BIOS downloads work in this mode. **Launching still needs EmuDeck** — the
game page says so, Play is disabled rather than failing, and card buttons install without the
"play" half. Naming an EmuDeck root in **Settings → EmuDeck** (or installing EmuDeck and rescanning)
switches the app back to EmuDeck's folders; a configured game folder takes precedence over
auto-detection, so the choice is always explicit.

**What RomMArcade does not do (yet):** save-state sync, Steam shortcut creation, ROM patching,
and multi-disc conversion. Those remain EmuDeck/RomM's business; this app deliberately keeps its
hands off files it did not create.

## Saves

RomMArcade syncs a game's battery save with RomM around every launch, using RomM's own save slots
(an `autosave` slot plus `autosave-<ext>` sidecars such as a `.rtc` clock file), so it pairs with
other RomM clients instead of inventing its own storage.

- **Before a launch** the app negotiates with the server: saves the server has and this machine
  does not are pulled into EmuDeck's `saves/<system>` folder first, local progress is uploaded,
  and only then does the emulator start. If a save cannot be restored, or changed on both sides,
  the launch stops with an explanation — the emulator never opens on a save older than the
  server's.
- **After the emulator exits** the save it wrote is pushed to RomM.
- **The game page's Saves panel** lists every version on RomM and every file on this machine.
  **Use & play** restores a chosen version here, promotes it to this game's sync slot and starts
  the game; **Use** does the same without launching. A row can also be favorited, labelled,
  renamed, made public, downloaded as a file, or deleted. Local files can be uploaded; the
  **Sync slot** control picks the slot this game pairs on, and **New slot** creates a named slot
  for the game (optionally seeded with the save on this machine) so checkpoints can live side by
  side.
- Settings live under `save_sync` in `config.json`: `enabled`, `play_gate` (set it to `false` to
  launch even when saving fails), and `conflicts` (`block` by default; `local`, `server` or
  `newest` choose a side automatically). A server that has no save-sync API is detected and the
  app plays normally.

Save-state (`states/`) sync is not covered; only battery saves.

## Development

```bash
python3 -m unittest tests.test_integration -v   # 56 tests, no network
ROMARCADE_HOME=/tmp/rommarcade-dev python3 -m rommarcade serve --port 8791
```

The test suite starts a fake RomM server, a throwaway EmuDeck tree, and a laptop-style game-folder
setup, then exercises pairing, browsing, the games-only filter, cover caching, installs
(single-file and multi-file zip), the mirrored RomM folder layout, BIOS, launching, the EmuDeck
on/off switch, and the deep-link queue end to end.

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
├── desktop.py      # native webview window and local service lifecycle
└── web/            # index.html, app.css, app.js (no framework, no build)

packaging/
├── build_desktop.py  # PyInstaller build for the current OS
├── RomMArcade.spec   # bundle recipe (app, .app, .exe)
└── install_desktop.sh
```

## License

MIT. RomMArcade is an independent client; RomM, ROMulus and EmuDeck remain their authors' work.
