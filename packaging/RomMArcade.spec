# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the RomMArcade desktop shell.

Run from the repository root with:
    python packaging/build_desktop.py
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ENTRY = os.path.join(ROOT, "packaging", "desktop_entry.py")
ICON_ICO = os.path.join(ROOT, "packaging", "rommarcade.ico")
ICON_ICNS = os.path.join(ROOT, "packaging", "rommarcade.icns")

datas = collect_data_files(
    "rommarcade",
    includes=["web/*", "web/css/*", "web/js/*"],
)
hiddenimports = collect_submodules("webview")
excludes = ["tkinter", "unittest", "tests"]
if sys.platform.startswith("linux"):
    hiddenimports += [
        "gi",
        "gi.repository.Gtk",
        "gi.repository.Gdk",
        "gi.repository.GLib",
        "gi.repository.Gio",
        "gi.repository.WebKit2",
    ]
    # The tested Linux backend is GTK WebKit.  Do not drag a second GUI
    # toolkit (and hundreds of megabytes) into the bundle because it happens
    # to be installed on the build machine.
    excludes += ["PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy"]

a = Analysis(
    [ENTRY],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform.startswith("linux"):
    # GTK's full Adwaita icon set and every locale are megabytes of data the
    # app never shows (its icons are inline SVG).  Keep the bundle lean.
    a.datas = [
        entry
        for entry in a.datas
        if not entry[0].startswith(("share/icons/", "share/locale/"))
    ]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RomMArcade",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON_ICO if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="RomMArcade",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="RomMArcade.app",
        icon=ICON_ICNS,
        bundle_identifier="org.koehlerkilleen.rommarcade",
        info_plist={
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
        },
    )
