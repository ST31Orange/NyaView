"""Self-test: launch the real window + real frontend, get DOM stats reported
back through the bridge (JS->Python, deadlock-safe), then close."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import webview

sys.path.insert(0, r"D:\AIHOME\CODEX\技术调研\NyaView\src")

from nyavview.ui.api import Api
from nyavview.ui.queue import JobManager

HTML = Path(__file__).resolve().parent / "src" / "nyavview" / "ui" / "static" / "index.html"

REPORT: dict = {}
DONE = threading.Event()


def main() -> None:
    manager = JobManager()
    manager.start()

    class SelftestApi(Api):
        def selftest_report(self, payload: dict) -> None:
            REPORT.update(payload or {})
            DONE.set()

    api = SelftestApi(manager)
    window = webview.create_window(
        title="NyaView self-test",
        url=f"{HTML}?selftest=1",
        js_api=api,
        width=1080,
        height=780,
        background_color="#FAF9F5",
    )
    api._window = window

    def watchdog() -> None:
        DONE.wait(15)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=watchdog, daemon=True).start()
    webview.start()
    manager.stop()
    REPORT["window_closed"] = True
    print("SELFTEST_RESULT:", REPORT)


if __name__ == "__main__":
    main()
