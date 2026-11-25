#!/usr/bin/env bash
# Install the built Linux desktop app for the current user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BINARY="${1:-$HERE/../dist/RomMArcade/RomMArcade}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA_HOME/rommarcade-desktop"
BIN_DIR="$HOME/.local/bin"
APPLICATIONS="$DATA_HOME/applications"

if [[ ! -x "$BINARY" ]]; then
  echo "Not found or not executable: $BINARY" >&2
  echo "Build it first with: python packaging/build_desktop.py" >&2
  exit 1
fi

if [[ "${ROMARCADE_KEEP_SERVICE:-0}" != "1" ]] && command -v systemctl >/dev/null 2>&1; then
  SYSTEMCTL=(systemctl --user)
  if [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    SYSTEMCTL=(systemctl --user)
  fi
  if "${SYSTEMCTL[@]}" is-active --quiet rommarcade.service 2>/dev/null; then
    echo "Stopping the browser service so the desktop app owns the port."
    "${SYSTEMCTL[@]}" disable --now rommarcade.service 2>/dev/null || true
  fi
fi

rm -rf "$DEST"
mkdir -p "$DEST" "$BIN_DIR" "$APPLICATIONS"
cp -a "$(dirname "$BINARY")/." "$DEST/"
ln -sf "$DEST/RomMArcade" "$BIN_DIR/rommarcade-desktop"

cat > "$APPLICATIONS/rommarcade.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=RomMArcade
Comment=Your RomM library, in an arcade shell
Exec=$BIN_DIR/rommarcade-desktop
Icon=applications-games
Terminal=false
Categories=Game;Emulator;
Keywords=romm;emudeck;emulator;games;arcade;
StartupNotify=true
StartupWMClass=RomMArcade
EOF

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS" >/dev/null 2>&1 || true
fi

echo "Installed RomMArcade desktop to $DEST"
echo "Launcher: $BIN_DIR/rommarcade-desktop"
echo "Config:   ${XDG_CONFIG_HOME:-$HOME/.config}/rommarcade"
