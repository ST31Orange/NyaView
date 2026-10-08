"""Desktop UI entry point (pywebview).

Run with ``python -m nyavview.ui.app`` or via the packaged EXE.
"""

from __future__ import annotations

import sys
from pathlib import Path

import webview

from nyavview.ui.api import Api, APP_NAME, load_user_settings
from nyavview.ui.queue import JobManager


def resource_path(relative: str) -> str:
    """Resolve a bundled asset path inside a PyInstaller frozen app."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return str(base / relative)


def main() -> None:
    # Start from the user-level settings (~/.nyavview.toml) when present.
    manager = JobManager(settings=load_user_settings())
    manager.start()
    api = Api(manager)
    window = webview.create_window(
        title=f"{APP_NAME} · 视频下载与转换",
        url=resource_path("static/index.html"),
        js_api=api,
        width=1080,
        height=780,
        min_size=(900, 660),
        background_color="#FAF9F5",
    )
    api.window = window
    try:
        webview.start(debug=False)
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
