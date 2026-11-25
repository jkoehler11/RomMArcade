#!/usr/bin/env bash
# Build a macOS installer package (.pkg) around the PyInstaller app bundle.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c 'import rommarcade; print(rommarcade.__version__)')"
APP="$ROOT/dist/RomMArcade.app"
OUTPUT="$ROOT/dist/RomMArcade-${VERSION}-macos-x86_64.pkg"
COMPONENT="$ROOT/build/RomMArcade-component.pkg"

if [[ ! -d "$APP" ]]; then
  echo "Missing app bundle: $APP" >&2
  echo "Run: python packaging/build_desktop.py" >&2
  exit 1
fi

mkdir -p "$(dirname "$COMPONENT")"
rm -f "$COMPONENT" "$OUTPUT"

pkgbuild \
  --component "$APP" \
  --install-location /Applications \
  --identifier org.koehlerkilleen.rommarcade \
  --version "$VERSION" \
  --ownership recommended \
  "$COMPONENT"

productbuild --package "$COMPONENT" "$OUTPUT"
rm -f "$COMPONENT"
echo "Built $OUTPUT"
