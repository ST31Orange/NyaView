# -*- mode: python ; coding: utf-8 -*-
"""Single-file EXE variant.

Build:
    uv run pyinstaller --noconfirm --clean nyavview-onefile.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "src" / "nyavview" / "ui" / "static"), "static"),
        (str(ROOT / "assets" / "nyavview.ico"), "."),
    ],
    hiddenimports=[
        "yt_dlp",
        "yt_dlp.utils",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
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
    a.binaries,
    a.datas,
    [],
    name="NyaView",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "nyavview.ico"),
)
