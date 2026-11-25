#!/bin/sh
# Remove RomMArcade's program files; config and downloaded state are kept.
set -eu

APP_DIR=${ROMARCADE_INSTALL_DIR:-"$HOME/.local/share/rommarcade"}
BIN_DIR=${ROMARCADE_BIN_DIR:-"$HOME/.local/bin"}
CONFIG_DIR=${XDG_CONFIG_HOME:-"$HOME/.config"}
SYSTEMD_DIR="$CONFIG_DIR/systemd/user"
APPS_DIR="$HOME/.local/share/applications"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now rommarcade.service >/dev/null 2>&1 || true
fi

rm -f "$SYSTEMD_DIR/rommarcade.service"
rm -f "$APPS_DIR/rommarcade.desktop" "$APPS_DIR/rommarcade-url.desktop"
rm -f "$BIN_DIR/rommarcade" "$BIN_DIR/rommarcade-open" "$BIN_DIR/rommarcade-launch"
rm -rf "$APP_DIR/app"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

echo "RomMArcade program files removed."
echo "Kept: $CONFIG_DIR/rommarcade and $APP_DIR/data (delete them by hand if you want a clean slate)."
