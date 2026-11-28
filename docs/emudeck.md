# EmuDeck integration

RomMArcade is a front end for an EmuDeck machine. EmuDeck owns the emulators, the ROM folders and
the launcher scripts; RomMArcade finds them and hands over the right file.

## The tree it expects

RomMArcade reads `~/.config/EmuDeck/settings.json` for `storagePath` and expects the usual tree:

```text
~/Emulation/
├── roms/<system>/          # installs land here
├── bios/                   # RomM firmware downloads land here
└── tools/launchers/*.sh    # the launchers RomMArcade starts
```

## Platform mapping

Platform folders are matched on RomM's `fs_slug` first, then by known aliases (`ngc → gc`,
`3ds → n3ds`, `dc → dreamcast`, `sega32 → sega32x`, `ps1 → psx`, …), then by a normalized name.

Every mapping is visible and editable in **Settings → EmuDeck**: folder, launcher, and libretro
core per platform. Overrides live in the RomMArcade config, not in EmuDeck.

Launcher choice follows the system: standalone emulators where EmuDeck installs them (Dolphin,
Cemu, DuckStation, PCSX2, PPSSPP, melonDS, Azahar, Flycast, Xenia), RetroArch otherwise. For
RetroArch, RomMArcade picks the first core that actually exists in the installed core folder
(`mesen` for NES, `snes9x` for SNES, `mgba` for GBA, `mupen64plus_next` for N64, `fbneo` for
arcade, …) and falls back to the launcher's own default if none is found.

Launches are detached from the service and inherit the graphical session (`XDG_RUNTIME_DIR`,
`WAYLAND_DISPLAY` by socket discovery), so closing the browser does not close the game.

Cemu needs its own `keys.txt` in Cemu's user-data folder, not only the copy under
`Emulation/bios/`. RomMArcade merges the EmuDeck key database into Cemu's file before launching a
Wii U game and keeps a timestamped backup, so encrypted WUD/WUX discs can be decrypted.

## The on/off switch

**Settings → EmuDeck** (or the EmuDeck page) toggles the whole integration. With it off, install,
play, BIOS, and launcher actions are refused by the API; the library remains browsable. The
default is on.

RomMArcade never installs or updates emulators itself. If something is missing, it opens EmuDeck
or links to the emulator's official site; it never installs software behind your back.

## Machines without EmuDeck

A laptop that never ran EmuDeck has no `Emulation` tree, so the EmuDeck step of the wizard becomes a
game-folder step instead. It offers two layouts and a way to defer:

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

## What RomMArcade does not do

- It does not auto-sync save states (`states/`); the **States** tab copies and uploads them
  manually. Battery saves are the ones synced around a launch.
- It does not create Steam shortcuts.
- It does not patch or convert ROMs.
- It does not do multi-disc conversion.

Those remain EmuDeck/RomM's business; this app deliberately keeps its hands off files it did not
create.
