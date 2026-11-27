# Configuration

RomMArcade keeps configuration per installation. There is no database and no cloud account.

## Server addresses

The setup wizard and **Settings → Server** accept:

```text
romm.local:7676                    → http://romm.local:7676
http://romm.lan:7676/              → http://romm.lan:7676
https://romm.example.com/romm/     → https://romm.example.com/romm
http://host:7676/api               → http://host:7676
```

A trailing `/api` is stripped, so the address from RomM's API docs works too. Local and private
hosts default to `http://`; public hostnames default to `https://`.

You can keep several servers and switch between them. Each server gets its own library and save
state.

## Pairing and credentials

RomM's device flow gives this machine its own revocable token. A pasted client token or HTTP Basic
credentials are also supported. Tokens are stored in the per-installation config directory.

## Deep links

```text
rommarcade://open?rom=180
rommarcade://open?rom=180&platform=nes&name=1942
rommarcade://open?rom=180&play=1
rommarcade://play?rom=180
```

`play=1` (or the `play` action) installs the game first when it is missing, then launches it. A
target is consumed once; reloading the page does not navigate again.

`packaging/install.sh` registers `x-scheme-handler/rommarcade`; `rommarcade-open` hands the URL to
the running service and starts it if necessary.

## The RomM plugin

The plugin that adds **Play in RomMArcade** to RomM's Play menu lives in
[jkoehler11/rommarcade-plugin](https://github.com/jkoehler11/rommarcade-plugin), together with the
`rommarcade://` handler for desktop installs that did not register one. Install it in RomM under
**Settings → Plugins**, or point the server at the raw manifest:

```text
https://raw.githubusercontent.com/jkoehler11/rommarcade-plugin/main/plugin.json
```

The plugin repository documents its settings (`&play=1` makes the button install and start a game
in one press; clearing it only opens the game) and the platforms an action can be limited to.
Nothing in it runs on the RomM server, and the app works without it.

## Files and data

Config and data are per installation:

- **Linux:** `~/.config/rommarcade` and `~/.local/share/rommarcade`
- **macOS:** `~/Library/Application Support/RomMArcade`
- **Windows:** `%APPDATA%\RomMArcade` and `%LOCALAPPDATA%\RomMArcade`

## One app, one machine

Because this is a desktop app, each computer runs its own copy and uses its own EmuDeck, ROM
folders and launchers. There is no shared browser profile and no remote service to point at. Two
desktops can browse the same RomM library while installing and playing against their own local
emulators.

## Headless mode

The older headless mode is still available for a Steam Deck, NAS or remote browser setup:

```bash
rommarcade serve --host 0.0.0.0 --port 8790
```

That mode is a local web service; it does not execute anything on the computer viewing the page.
