#!/usr/bin/env bash
# Build RomMArcade as a single-file Linux AppImage from the PyInstaller output.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c 'import rommarcade; print(rommarcade.__version__)')"
BUNDLE="$ROOT/dist/RomMArcade"
OUTPUT="$ROOT/dist/RomMArcade-${VERSION}-linux-x86_64.AppImage"
APPDIR="$ROOT/build/appimage/RomMArcade.AppDir"

if [[ ! -x "$BUNDLE/RomMArcade" ]]; then
  echo "Missing PyInstaller bundle: $BUNDLE/RomMArcade" >&2
  echo "Run: python packaging/build_desktop.py" >&2
  exit 1
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp -a "$BUNDLE" "$APPDIR/usr/lib/rommarcade"
# PyInstaller collects the GTK/WebKit stack it sees at build time.  Mixing
# those libraries with a different distro's WebKit breaks at load time, so
# keep Python private and let AppImage use the host's GTK/WebKit packages.
find "$APPDIR/usr/lib/rommarcade/_internal" -maxdepth 1 -name 'lib*.so*' \
  ! -name 'libpython*.so*' -delete
rm -rf "$APPDIR/usr/lib/rommarcade/_internal/gi_typelibs"
cp "$HERE/rommarcade.png" "$APPDIR/rommarcade.png"
cp "$HERE/rommarcade.png" "$APPDIR/.DirIcon"
cp "$HERE/rommarcade.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/rommarcade.svg"
cp "$HERE/rommarcade.svg" "$APPDIR/rommarcade.svg"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/rommarcade/RomMArcade" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/rommarcade.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=RomMArcade
Comment=Your RomM library, in an arcade shell
Exec=RomMArcade
Icon=rommarcade
Terminal=false
Categories=Game;Emulator;
Keywords=romm;emudeck;emulator;games;arcade;
StartupNotify=true
StartupWMClass=RomMArcade
EOF
cp "$APPDIR/rommarcade.desktop" "$APPDIR/usr/share/applications/rommarcade.desktop"

TOOL="${APPIMAGETOOL:-$ROOT/build/appimagetool-x86_64.AppImage}"
if [[ ! -x "$TOOL" ]]; then
  mkdir -p "$(dirname "$TOOL")"
  curl -fL --retry 3 -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi

rm -f "$OUTPUT"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" "$APPDIR" "$OUTPUT"
chmod +x "$OUTPUT"
echo "Built $OUTPUT"
