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
    "max_concurrent_tasks",
)


def load_user_settings() -> Settings:
    if USER_CONFIG_PATH.is_file():
        return Settings.discover(USER_CONFIG_PATH)
    return Settings.discover()


def save_user_settings(settings: Settings) -> None:
    """Persist the UI-relevant subset to ``~/.nyavview.toml``."""
    lines = ["# NyaView user settings (managed by the desktop UI)\n"]
    values = settings.model_dump()
    for key in _UI_KEYS:
        value = values.get(key)
        if value is None:
            continue
        if isinstance(value, Path):
            lines.append(f'{key} = "{value}"\n')
        elif isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}\n")
        else:
            lines.append(f"{key} = {value!r}\n")
    USER_CONFIG_PATH.write_text("".join(lines), encoding="utf-8")


class Api:
    def __init__(self, manager: JobManager) -> None:
        self.manager = manager
        self.window: Optional[webview.Window] = None  # injected by pywebview

    # ----------------------------------------------------------- runtime

    def runtime_info(self) -> dict[str, Any]:
        try:
            import yt_dlp.version

            ytdlp = yt_dlp.version.__version__
        except Exception:
            ytdlp = "?"
        settings = self.manager.settings
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
                elif key == "max_concurrent_tasks":
                    setattr(settings, key, int(value))
                else:
                    setattr(settings, key, value)
            save_user_settings(settings)
            self.manager.settings = settings
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ----------------------------------------------------------- dialogs

    def pick_file(self, title: str = "选择文件") -> Optional[str]:
        if self.window is None:
            return None
        result = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=("视频文件 (*.mp4;*.mkv;*.mov;*.avi;*.flv;*.ts;*.webm)", "所有文件 (*.*)")
        )
        if not result:
            return None
        return str(result[0])

    def pick_folder(self, title: str = "选择文件夹") -> Optional[str]:
        if self.window is None:
            return None
        result = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return str(result[0])

    def pick_output_dir(self, title: str = "选择输出目录") -> Optional[str]:
        return self.pick_folder(title)

    # -------------------------------------------------------------- jobs

    def start_download(self, url: str, options: dict[str, Any]) -> dict[str, Any]:
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "请输入下载链接"}
        try:
            job_id = self.manager.submit_download(url, options or {})
            return {"ok": True, "jobId": job_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_convert(self, source: str, options: dict[str, Any]) -> dict[str, Any]:
        if not source:
            return {"ok": False, "error": "请先选择要转换的文件"}
        try:
            job_id = self.manager.submit_convert(source, options or {})
            return {"ok": True, "jobId": job_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_bili(self, source: str, options: dict[str, Any]) -> dict[str, Any]:
        if not source:
            return {"ok": False, "error": "请先选择 Bilibili 缓存文件夹"}
        try:
            job_id = self.manager.submit_bili(source, options or {})
            return {"ok": True, "jobId": job_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def cancel(self, job_id: str) -> dict[str, Any]:
        ok = self.manager.cancel(job_id)
        return {"ok": ok, "jobId": job_id}

    def clear_finished(self) -> dict[str, Any]:
        count = self.manager.clear_finished()
        return {"ok": True, "removed": count}

    def jobs(self) -> list[dict[str, Any]]:
        return self.manager.list_jobs()

    def poll(self) -> list[dict[str, Any]]:
        return self.manager.poll()
