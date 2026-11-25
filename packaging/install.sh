#!/bin/sh
# RomMArcade installer: no pip, no build step, no root.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC=$(CDPATH= cd -- "$HERE/.." && pwd)

APP_DIR=${ROMARCADE_INSTALL_DIR:-"$HOME/.local/share/rommarcade"}
BIN_DIR=${ROMARCADE_BIN_DIR:-"$HOME/.local/bin"}
CONFIG_DIR=${XDG_CONFIG_HOME:-"$HOME/.config"}
SYSTEMD_DIR="$CONFIG_DIR/systemd/user"
APPS_DIR="$HOME/.local/share/applications"
PYTHON=${PYTHON:-$(command -v python3 || true)}
PORT=${ROMARCADE_PORT:-8790}
WITH_SERVICE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --no-service) WITH_SERVICE=0 ;;
    --port) shift; PORT=${1:?"--port needs a value"} ;;
    --help|-h)
      echo "usage: install.sh [--no-service] [--port 8790]"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "$PYTHON" ]; then
  echo "RomMArcade needs python3 (3.10 or newer)." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "RomMArcade needs Python 3.10 or newer; $PYTHON is $("$PYTHON" -V 2>&1)." >&2
  exit 1
fi

echo "Installing RomMArcade into $APP_DIR"
rm -rf "$APP_DIR/app"
mkdir -p "$APP_DIR/app"
cp -R "$SRC/rommarcade" "$APP_DIR/app/rommarcade"
find "$APP_DIR/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$APP_DIR/data" "$APP_DIR/config" "$BIN_DIR" "$APPS_DIR"

cat > "$BIN_DIR/rommarcade" <<EOF
#!/bin/sh
PYTHONPATH="$APP_DIR/app\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PYTHON" -m rommarcade "\$@"
EOF
chmod 755 "$BIN_DIR/rommarcade"

cat > "$BIN_DIR/rommarcade-open" <<EOF
#!/bin/sh
# Hand a rommarcade:// URL to the service, starting it first if needed.
url="\$1"
[ -n "\$url" ] || { echo "usage: rommarcade-open rommarcade://open?rom=ID" >&2; exit 2; }
if "$BIN_DIR/rommarcade" open "\$url" >/dev/null 2>&1; then exit 0; fi
systemctl --user start rommarcade.service >/dev/null 2>&1 || true
i=0
while [ \$i -lt 40 ]; do
  sleep 0.25
  if "$BIN_DIR/rommarcade" open "\$url" >/dev/null 2>&1; then exit 0; fi
  i=\$((i + 1))
done
echo "RomMArcade did not start. Run: systemctl --user status rommarcade" >&2
exit 1
EOF
chmod 755 "$BIN_DIR/rommarcade-open"

cat > "$BIN_DIR/rommarcade-launch" <<EOF
#!/bin/sh
# Open the web UI, starting the service if it is not running.
systemctl --user start rommarcade.service >/dev/null 2>&1 || true
i=0
while [ \$i -lt 40 ]; do
  if "$BIN_DIR/rommarcade" status >/dev/null 2>&1; then break; fi
  sleep 0.25
  i=\$((i + 1))
done
port=\$("$PYTHON" -c 'import json, os; p=os.path.expanduser("~/.config/rommarcade/config.json"); print(json.load(open(p)).get("web", {}).get("port", 8790) if os.path.exists(p) else 8790)' 2>/dev/null || echo 8790)
exec xdg-open "http://127.0.0.1:\$port/"
EOF
chmod 755 "$BIN_DIR/rommarcade-launch"

# First-run config with the chosen port (never overwrite an existing one).
if [ ! -f "$CONFIG_DIR/rommarcade/config.json" ]; then
  mkdir -p "$CONFIG_DIR/rommarcade"
  "$PYTHON" - "$CONFIG_DIR/rommarcade/config.json" "$PORT" <<'PY'
import json, sys
path, port = sys.argv[1], int(sys.argv[2])
data = {"version": 1, "web": {"bind": "0.0.0.0", "port": port}}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  chmod 600 "$CONFIG_DIR/rommarcade/config.json"
fi

# Desktop entries.
sed "s|__BIN__|$BIN_DIR|g" "$HERE/rommarcade.desktop" > "$APPS_DIR/rommarcade.desktop"
sed "s|__BIN__|$BIN_DIR|g" "$HERE/rommarcade-url.desktop" > "$APPS_DIR/rommarcade-url.desktop"
chmod 644 "$APPS_DIR/rommarcade.desktop" "$APPS_DIR/rommarcade-url.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi
if command -v xdg-mime >/dev/null 2>&1; then
  xdg-mime default rommarcade-url.desktop x-scheme-handler/rommarcade 2>/dev/null || true
fi

if [ "$WITH_SERVICE" -eq 1 ] && command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$SYSTEMD_DIR"
  cat > "$SYSTEMD_DIR/rommarcade.service" <<EOF
[Unit]
Description=RomMArcade - RomM front-end for EmuDeck
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR/app
Environment=PYTHONPATH=$APP_DIR/app
Environment=XDG_RUNTIME_DIR=/run/user/$(id -u)
ExecStart="$PYTHON" -m rommarcade serve
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if systemctl --user enable --now rommarcade.service >/dev/null 2>&1; then
    echo "Service enabled: rommarcade.service"
  else
    echo "Could not start the user service from here. Run:"
    echo "  systemctl --user enable --now rommarcade.service"
  fi
fi

echo
echo "RomMArcade is installed."
echo "  Open:    http://127.0.0.1:$PORT"
echo "  Command: rommarcade status"
echo "  Config:  $CONFIG_DIR/rommarcade/config.json"
echo "  Data:    $HOME/.local/share/rommarcade"
echo
echo "If ~/.local/bin is not on PATH, add: export PATH=\"\$HOME/.local/bin:\$PATH\""
