# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for NyaView desktop app.

Build:
    uv run pyinstaller --noconfirm nyavview.spec
"""

import os
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "src" / "nyavview" / "ui" / "static"), "nyavview/ui/static"),
        (str(ROOT / "assets" / "nyavview.ico"), "."),
    ],
    hiddenimports=[
        # yt-dlp is imported lazily inside YtDlpEngine._run(); PyInstaller
        # static analysis cannot see it, so bundle it explicitly.
        "yt_dlp",
        "yt_dlp.utils",
        # webview backends are loaded dynamically.
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        # imageio-ffmpeg ships the ffmpeg binary as data.
        "imageio_ffmpeg.binaries",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc", "doctest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NyaView",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "nyavview.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NyaView",
)
