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
    """Resolve a bundled asset path inside a PyInstaller frozen app.

    The PyInstaller specs bundle the frontend as the datas target ``static``,
    so files land at ``<base>/static/`` where ``<base>`` is ``sys._MEIPASS``
    (both onefile and onedir PyInstaller 6 layouts set it) — and in development
    the file sits next to this module (``nyavview/ui/static``).
    """
    candidates: list[Path] = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / relative)
    candidates.append(Path(__file__).resolve().parent / relative)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    # Fall back to the frozen base even if not found yet (clearer 404 than
    # a FileNotFoundError here).
    return str(candidates[0])


def _install_debug_hook() -> None:
    """TEMP debug: log pywebview's local-server behavior to a file.

    Enabled only when NYAVIEW_DEBUG_SERVER=1 (used to diagnose 404s).
    """
    import os

    if os.environ.get("NYAVIEW_DEBUG_SERVER") != "1":
        return
    import bottle

    log_path = Path(os.environ.get("NYAVIEW_DEBUG_LOG", str(Path.home() / "nyaview_debug.log")))

    def _log(msg: str) -> None:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")

    import webview.util as wv_util

    _orig_static = bottle.static_file

    def patched_static(filename, root, **kw):
        resolved = Path(root) / filename
        _log(f"static_file filename={filename!r} root={root!r} exists={resolved.exists()}")
        return _orig_static(filename, root, **kw)

    bottle.static_file = patched_static

    from webview import http as wv_http

    _orig_start = wv_http.BottleServer.start_server

    @classmethod
    def patched_start(cls, urls, http_port=None, keyfile=None, certfile=None):
        result = _orig_start(urls, http_port, keyfile, certfile)
        address, common_path, server = result
        _log(
            f"start_server urls={urls!r} address={address!r} "
            f"common_path={common_path!r} root_path={server.root_path!r}"
        )
        return result

    wv_http.BottleServer.start_server = patched_start
    _log(f"debug hook installed; get_app_root via util=")
    try:
        _log(f"  util.get_app_root() = {wv_util.get_app_root()!r}")
        _log(f"  hasattr(sys,'_MEIPASS') = {hasattr(sys, '_MEIPASS')}")
        _log(f"  sys.argv[0] = {sys.argv[0]!r}")
    except Exception as exc:  # noqa: BLE001
        _log(f"  get_app_root failed: {exc!r}")


def _setup_drop_handlers(window: Any, manager: JobManager) -> None:
    """Register pywebview DOM drop handlers for the convert drop zones.

    Plain JS ``addEventListener('drop')`` receives only a relative path for
    files/folders dropped into a WebView2 (the full-path injection requires
    ``_dnd_state['num_listeners'] > 0``, which only happens when a drop
    handler is registered through pywebview's DOM event API). These handlers
    run on the Python side with the absolute ``pywebviewFullPath`` and hand it
    to the JS frontend via the poll channel (safe: no Python→JS evaluate_js,
    which would deadlock while the page polls).
    """

    def _make_handler(target: str):
        def _handle(event: dict) -> None:
            try:
                path = None
                files = (event.get("dataTransfer") or {}).get("files") or []
                for f in files:
                    path = f.get("pywebviewFullPath") or f.get("path") or path
                if path:
                    manager.set_pending_drop(target, str(path))
            except Exception as exc:  # defensive: never crash the bridge thread
                manager.set_pending_drop(target, "")

        return _handle

    try:
        window.dom.get_element("#cvDropZone").events.drop += _make_handler("cv")
        window.dom.get_element("#biliDropZone").events.drop += _make_handler("bili")
    except Exception as exc:  # noqa: BLE001 - DOM setup is best-effort
        print(f"[drop] failed to register DOM drop handlers: {exc!r}")


def main() -> None:
    _install_debug_hook()
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
    api._window = window

    def _on_ready() -> None:
        _setup_drop_handlers(window, manager)

    try:
        webview.start(_on_ready, debug=False)
    finally:
        manager.stop()


if __name__ == "__main__":
    main()
