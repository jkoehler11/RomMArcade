# Saves

RomMArcade keeps a game's battery save in step with RomM around every launch. It uses RomM's own
save slots — an `autosave` slot plus `autosave-<ext>` sidecars such as a `.rtc` clock file — so it
pairs with other RomM clients instead of inventing its own storage.

## Around a launch

- **Before a launch** the app negotiates with the server: saves the server has and this machine
  does not are pulled into the emulator's save folder first, local progress is uploaded, and only
  then does the emulator start.
- **After the emulator exits** the save it wrote is pushed to RomM.
- If a save cannot be restored, or changed on both sides, the launch stops with an explanation —
  the emulator never opens on a save older than the server's.
- A green **Synced** badge appears in the game header and Saves panel when the current server save
  and the local file have the same content — whether the version came from RomM or was uploaded
  from this machine.

## The Saves panel

The game page's **Saves** tab lists every version on RomM and every file on this machine.

- **Use & play** restores a chosen version here, promotes it to this game's sync slot and starts
  the game.
- **Use** does the same without launching.
- A row can be favorited, labelled, renamed, made public, downloaded as a file, or deleted.
- Local files can be uploaded to RomM.
- **Sync slot** picks the slot this game pairs on; **New slot** creates a named slot for the game
  (optionally seeded with the save on this machine) so checkpoints can live side by side.

## Save states

Save states are emulator snapshots, not the battery save a game writes for itself. The game page's
**States** tab lists:

- state files in the mapped emulator's EmuDeck state folder that belong to this game;
- states RomM holds for the game, with the emulator and core that wrote them.

**Use** copies one RomM state into the emulator's state folder under the game's plain state name and
backs up the previous file first. **Upload** sends a local state to RomM. Nothing is pulled automatically before a launch: a state can be tied to a particular
emulator, core and version, so silently replacing it during launch would be unsafe. Load a
copied state from the emulator's own state menu.

For RetroArch the folder is `saves/retroarch/states`; melonDS uses `saves/melonds/states`. Other
launchers fall back to `saves/<platform>/states`, and the States tab shows the exact path it used.

## Emulator coverage

Save sync currently supports emulators that keep one flat save file per game:

- **RetroArch** platforms: arcade, Atari 2600/5200, Game Gear, GBA, Genesis, N64, NDS, NES, Saturn,
  SNES, and other RetroArch-backed systems.
- **Standalone melonDS** when it is mapped as the launcher.

Structured emulators — Dolphin, Ryujinx, Azahar, Cemu, Xenia, Flycast, PPSSPP, RPCS3 and similar —
store saves in title-ID trees, not `<rom>.<ext>` files. RomMArcade marks save sync as unavailable
for those rather than writing into a folder the emulator ignores. Save states are handled separately
in the **States** tab as manual copies/uploads; they are not auto-synced.

## Settings

Save behaviour lives under `save_sync` in `config.json`:

- `enabled` — turn save sync on or off.
- `play_gate` — set it to `false` to launch even when saving fails.
- `conflicts` — `block` by default; `local`, `server` or `newest` choose a side automatically.

A server that has no save-sync API is detected and the app plays normally.
