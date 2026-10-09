"""JS bridge API exposed to the webview frontend.

pywebview injects the window into ``self.window`` when the instance is passed
via ``js_api=``, so file dialogs can be opened from here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import webview

from nyavview.core.errors import ConfigurationError
from nyavview.core.settings import Settings
from nyavview.ui.queue import JobManager

APP_NAME = "NyaView"
APP_VERSION = "0.1.0"

USER_CONFIG_PATH = Path.home() / ".nyavview.toml"

_UI_KEYS = (
    "download_dir",
    "converted_dir",
    "cookie_file",
    "cookies_from_browser",
    "audio_bitrate",
    "video_preset",
    "video_crf",
    "max_concurrent_tasks",
    "naming_template",
    "dark_mode",
)


def load_user_settings() -> Settings:
    if USER_CONFIG_PATH.is_file():
        return Settings.discover(USER_CONFIG_PATH)
    return Settings.discover()


def _toml_string(value: str) -> str:
    """Quote a string for a TOML basic string, escaping backslashes/quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_user_settings(settings: Settings) -> None:
    """Persist the UI-relevant subset to ``~/.nyavview.toml``.

    Values must be escaped for TOML — on Windows ``str(Path)`` yields
    backslashes (``D:\\videos``) which are illegal unescaped in TOML.
    """
    lines = ["# NyaView user settings (managed by the desktop UI)\n"]
    values = settings.model_dump()
    for key in _UI_KEYS:
        value = values.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}\n")
        elif isinstance(value, int):
            lines.append(f"{key} = {value}\n")
        else:
            lines.append(f"{key} = {_toml_string(str(value))}\n")
    USER_CONFIG_PATH.write_text("".join(lines), encoding="utf-8")


class Api:
    # NOTE: internal references are underscore-prefixed on purpose. pywebview's
    # get_functions() recursively walks every non-underscore attribute of the
    # js_api object to build the JS bridge; walking into the JobManager /
    # webview Window / pydantic Settings object graph could hang (it did in the
    # frozen EXE). Underscore attrs are skipped, so the bridge exposes only the
    # real API methods below.

    def __init__(self, manager: JobManager) -> None:
        self._manager = manager
        self._window: Optional[webview.Window] = None  # injected after create_window

    # ----------------------------------------------------------- runtime

    def runtime_info(self) -> dict[str, Any]:
        try:
            import yt_dlp.version

            ytdlp = yt_dlp.version.__version__
        except Exception:
            ytdlp = "?"
        settings = self._manager.settings
        return {
            "name": APP_NAME,
            "version": APP_VERSION,
            "ffmpeg": settings.resolve_ffmpeg(),
            "ytdlp": ytdlp,
            "downloadDir": str(settings.download_dir),
            "convertedDir": str(settings.converted_dir),
        }

    def get_settings(self) -> dict[str, Any]:
        settings = load_user_settings()
        data = settings.model_dump()
        payload: dict[str, Any] = {}
        for key in _UI_KEYS:
            value = data.get(key)
            if isinstance(value, Path):
                value = str(value)
            payload[key] = value
        return payload

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = load_user_settings()
        updates = dict(payload)
        try:
            for key, value in updates.items():
                if key not in _UI_KEYS or value is None:
                    continue
                current = getattr(settings, key)
                if isinstance(current, Path) and not isinstance(value, Path):
                    setattr(settings, key, Path(str(value)))
                elif isinstance(current, bool):
                    setattr(settings, key, bool(value))
                elif isinstance(current, int):
                    setattr(settings, key, int(value))
                else:
                    setattr(settings, key, value)
            save_user_settings(settings)
            self._manager.settings = settings
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ----------------------------------------------------------- cookies

    COOKIE_CACHE_PATH = Path.home() / ".nyavview_cookies.txt"

    @staticmethod
    def _running_browser_processes(browser: str) -> int:
        """Count running processes for a browser (0 if none detected)."""
        names = {
            "edge": "msedge.exe",
            "chrome": "chrome.exe",
            "firefox": "firefox.exe",
        }
        exe = names.get(browser.lower(), "msedge.exe")
        try:
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return sum(1 for line in out.stdout.splitlines() if exe.lower() in line.lower())
        except Exception:
            return -1  # unknown

    @staticmethod
    def _extract_browser_cookies(browser: str) -> tuple[Any, list[str], int]:
        """Extract cookies from every profile of ``browser``.

        Returns ``(jar, errors, profile_count)``. Iterating profiles means a
        locked one (browser running) no longer fails the whole export.
        """
        from pathlib import Path as _Path

        from yt_dlp.cookies import (
            CHROMIUM_BASED_BROWSERS,
            YoutubeDLCookieJar,
            _get_chromium_based_browser_settings,
            extract_cookies_from_browser,
        )

        jar = YoutubeDLCookieJar()
        errors: list[str] = []
        profile_names: list[Any] = [None]  # None -> yt-dlp picks newest

        if browser.lower() in CHROMIUM_BASED_BROWSERS:
            try:
                browser_dir = _Path(_get_chromium_based_browser_settings(browser)["browser_dir"])
                if browser_dir.is_dir():
                    names = [
                        d.name
                        for d in browser_dir.iterdir()
                        if d.is_dir()
                        and ((d / "Cookies").is_file() or (d / "Network" / "Cookies").is_file())
                    ]
                    if names:
                        profile_names = sorted(names)
            except Exception:
                pass

        for prof in profile_names:
            try:
                sub = extract_cookies_from_browser(browser, profile=prof)
                for c in sub:
                    jar.set_cookie(c)
            except Exception as exc:  # locked / undecryptable profile — try next
                errors.append(f"{browser}/{prof or 'default'}: {type(exc).__name__}: {exc}")
        return jar, errors, len(profile_names)

    def load_cookies(self, browser: str = "") -> dict[str, Any]:
        """Export cookies from a browser into a persistent cache file.

        Tries the configured browser first; when its cookies are unreadable
        (Chromium App-Bound Encryption / v20 — closing the browser does NOT
        help) it automatically falls back to other installed browsers, where
        Firefox always works because it stores cookies unencrypted. Once
        exported, ``_opts`` prefers the cached cookie file.
        """
        primary = (browser or self._manager.settings.cookies_from_browser or "edge").strip()
        order = [primary] + [b for b in ("firefox", "chrome", "edge") if b != primary.lower()]

        jar = None
        used = primary
        errors: list[str] = []
        profiles = 0
        app_bound = False
        for candidate in order:
            sub, errs, profs = self._extract_browser_cookies(candidate)
            errors.extend(errs)
            if "decrypt" in " ".join(errs).lower() or "dpapi" in " ".join(errs).lower():
                app_bound = True
            if len(list(sub)) > 0:
                jar, used, profiles = sub, candidate, profs
                break

        if jar is None:
            n_running = self._running_browser_processes(primary)
            if app_bound:
                # Chromium App-Bound Encryption (v20): the key is sealed to the
                # browser's own code signature, so NO other process can decrypt
                # it — opening or closing the browser makes no difference.
                return {
                    "ok": False,
                    "error": f"{primary} 的 Cookie 使用了 App-Bound 加密（v20），任何程序（不只是本程序）都无法读取。",
                    "detail": "这是 Chromium 的新安全机制，关闭浏览器也没用。",
                    "hint": "两种可行做法：① 在 Firefox 里登录 B站 后，在「设置」把浏览器选为 Firefox 再点 🍪（Firefox 的 Cookie 可读取）；② 用浏览器插件（如「Get cookies.txt LOCALLY」）导出 cookies.txt，在「设置」页「Cookie 文件」选择该文件。",
                }
            detail = f"检测到 {n_running} 个 {primary} 进程正在运行。" if n_running > 0 else ""
            return {
                "ok": False,
                "error": "未能从浏览器读取任何 Cookie。",
                "detail": detail,
                "hint": "请先彻底关闭浏览器（包括托盘/后台进程），或在设置页填写浏览器插件导出的 cookies.txt。",
            }

        path = self.COOKIE_CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        jar.save(str(path), ignore_discard=True, ignore_expires=True)
        # Switch settings to the cache file so it is used from now on.
        settings = load_user_settings()
        settings.cookie_file = path
        settings.cookies_from_browser = None
        save_user_settings(settings)
        self._manager.settings = settings
        count = len(list(jar))
        return {
            "ok": True,
            "path": str(path),
            "count": count,
            "browser": used,
            "requested": primary,
            "switched": used != primary,
            "profiles": profiles,
            "skipped": len([e for e in errors if e.startswith(f"{used}/")]),
        }

    # ----------------------------------------------------------- dialogs

    def pick_file(self, title: str = "选择文件") -> Optional[str]:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=("视频文件 (*.mp4;*.mkv;*.mov;*.avi;*.flv;*.ts;*.webm)", "所有文件 (*.*)")
        )
        if not result:
            return None
        return str(result[0])

    def pick_folder(self, title: str = "选择文件夹") -> Optional[str]:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return str(result[0])

    def pick_output_dir(self, title: str = "选择输出目录") -> Optional[str]:
        return self.pick_folder(title)

    # -------------------------------------------------------------- ebook

    def scan_images(self, folder: str, mode: str = "merge") -> dict[str, Any]:
        """List an image pack in detected page order (for the ebook tab)."""
        from nyavview.engines.ebook import EbookBuilder

        folder = (folder or "").strip()
        if not folder:
            return {"ok": False, "error": "请先选择图片文件夹"}
        try:
            builder = EbookBuilder(self._manager.settings)
            info = builder.scan(Path(folder), mode="per_folder" if mode == "per_folder" else "merge")
            info["ok"] = True
            return info
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def ebook_thumbnails(self, paths: list[str], size: int = 150) -> dict[str, Any]:
        """Inline thumbnails for the reorder UI (local paths cannot be shown
        directly by the webview)."""
        from nyavview.engines.ebook import EbookBuilder

        try:
            builder = EbookBuilder(self._manager.settings)
            size = max(48, min(int(size or 150), 320))
            thumbs = builder.thumbnails([Path(p) for p in (paths or [])], size=size)
            return {"ok": True, "thumbs": thumbs}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_ebook(self, source: str, options: dict[str, Any]) -> dict[str, Any]:
        if not source:
            return {"ok": False, "error": "请先选择图片文件夹"}
        try:
            job_id = self._manager.submit_ebook(source, options or {})
            return {"ok": True, "jobId": job_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # -------------------------------------------------------------- jobs

    def preview(self, url: str, prefer_h264: bool = True) -> dict[str, Any]:
        """Extract title/thumbnail/formats without downloading (C/E).

        pywebview's bridge runs js_api methods synchronously in a background
        thread and JSON-serializes the return value (it does NOT await
        coroutines), so this stays synchronous: the async engine preview runs
        on a fresh event loop here, blocking only the bridge thread.
        """
        import asyncio

        from nyavview.engines.ytdlp import YtDlpEngine

        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "请输入链接"}
        engine = YtDlpEngine(self._manager.settings)
        try:
            info = asyncio.run(engine.preview(url, prefer_h264=bool(prefer_h264)))
            info["ok"] = True
            return info
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_download(self, url: str, options: dict[str, Any]) -> dict[str, Any]:
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "请输入下载链接"}
        try:
            job_id = self._manager.submit_download(url, options or {})
            return {"ok": True, "jobId": job_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_convert(self, source: str, options: dict[str, Any]) -> dict[str, Any]:
        if not source:
            return {"ok": False, "error": "请先选择要转换的文件"}
        try:
            job_id = self._manager.submit_convert(source, options or {})
            return {"ok": True, "jobId": job_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_bili(self, source: str, options: dict[str, Any]) -> dict[str, Any]:
        if not source:
            return {"ok": False, "error": "请先选择 Bilibili 缓存文件夹"}
        try:
            job_id = self._manager.submit_bili(source, options or {})
            return {"ok": True, "jobId": job_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def cancel(self, job_id: str) -> dict[str, Any]:
        ok = self._manager.cancel(job_id)
        return {"ok": ok, "jobId": job_id}

    def clear_finished(self) -> dict[str, Any]:
        count = self._manager.clear_finished()
        return {"ok": True, "removed": count}

    def jobs(self) -> list[dict[str, Any]]:
        return self._manager.list_jobs()

    def poll(self) -> list[dict[str, Any]]:
        return self._manager.poll()

    def open_folder(self, path: str) -> dict[str, Any]:
        """Open a folder in Explorer (F: 完成后打开所在文件夹)."""
        import os
        import subprocess

        target = Path(path).expanduser()
        folder = target if target.is_dir() else target.parent
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
