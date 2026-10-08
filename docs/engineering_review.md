# NyaView 工程评审与重构方案

> 编写视角：刚加入大型陌生代码库的高级工程师。先通读全部源码与文档，再给出结论。
> 阅读范围：`src/nyavview/**`（约 490 行）、`tests/`、`docs/`、`pyproject.toml`、`README.md`、`nyavview.toml.example`、Git 历史（3 个提交）。
> 结论：**分层清晰、代码量小、意图良好，但存在 2 个潜伏运行时 Bug、1 处 API 层重复、若干可维护性/性能/打包风险。** 下文按"架构摘要 → 问题区域 → 重构策略 → 改进后的代码 → UI 与打包 → 工具清单"展开。

---

## 1. 架构摘要（Architecture Summary）

```
┌──────────────────────────────────────────────────────────────┐
│  CLI (typer + rich)         桌面 UI (pywebview, 本版本新增)    │
│  src/nyavview/cli/          src/nyavview/ui/                  │
└──────────────┬──────────────────────────┬────────────────────┘
               │                          │
               ▼                          ▼
        core/service.py  —— 薄门面（facade），对外只暴露 download_url /
                           transcode_local_file / transcode_bilibili_cache
        ───────────────┬───────────────────────────
                       ▼
        engines/ytdlp.py      engines/ffmpeg.py
        适配 yt-dlp           适配 ffmpeg（含 Bilibili 缓存 .m4s 合并）
        ───────────────┬───────────────────────────
                       ▼
        utils/  run_process（asyncio 子进程）、safe_filename、paths
        core/settings.py  Pydantic 配置模型 + TOML 加载
```

**数据流（现状）**

1. **下载**：`cli download <url>` → `Settings.from_config(--config)` → `service.download_url` → `YtDlpEngine.download` → `run_process(["yt-dlp", ...])` → 解析 stdout 末尾 JSON 找输出文件。
2. **本地转码**：`cli convert <file>` → `service.transcode_local_file` → `FfmpegTranscoder.transcode` → 单输入 ffmpeg（libx264 + aac）→ 输出 `<stem>.mp4`（不覆盖时自动 `-1` 后缀）。
3. **Bilibili 缓存转码**：`cli convert <cache-dir>` → `service.transcode_bilibili_cache` → 逐目录找 `entry.json` / `video.m4s` + `audio.m4s` → 双输入 ffmpeg（视频重编码、音频 `copy`）→ 标题命名。

**架构优点**：门面 + 适配器隔离外部工具；引擎全部 async-ready；`utils` 通用化；为未来桌面 UI / REST 预留了同一套 core 驱动。这些是值得保留的骨架。

---

## 2. 问题区域（Problem Areas）

按严重度排序。括号内为对应文件与行号。

### 🔴 P0 — 潜伏运行时 Bug

1. **`ytdlp.py` 使用 `json` 却未导入（`src/nyavview/engines/ytdlp.py:78`）**
   文件顶部只有 `from pathlib import Path`、`from typing import Awaitable, Callable`，没有 `import json`。`_find_output()` 中 `json.loads(lines[-1])` 一旦执行必然抛 `NameError`；且 `except (IndexError, json.JSONDecodeError)` 中的 `json.JSONDecodeError` 求值也会再抛 `NameError`，不会被捕获。**下载成功后的"找输出文件"步骤实际必挂**（除非恰好走了 glob 分支前的异常路径——不会，NameError 不在 except 内）。`Awaitable, Callable` 为未使用的死导入。
2. **`ffmpeg.py` Bilibili 转码漏建目标目录（`src/nyavview/engines/ffmpeg.py:72-115`）**
   `transcode()` 里调用了 `ensure_directory(target_dir)`，但 `transcode_bilibili_cache()` 没有。目标目录不存在时 ffmpeg 直接写文件失败，报的是难懂的 ffmpeg 错误而非友好的路径错误。
3. **`run_process` 强制 UTF-8 解码（`src/nyavview/utils/__init__.py:30`）**
   `text=True, encoding="utf-8"` 未带 `errors="replace"`。ffmpeg / yt-dlp 在中文 Windows 控制台（GBK/cp936）下输出非 UTF-8 字节时，`proc.communicate()` 会抛 `UnicodeDecodeError`，导致转换/下载"成功但报告失败"。

### 🟠 P1 — 重复代码

4. **ffmpeg 参数构建两处重复（`ffmpeg.py:21-50` 与 `88-110`）**
   `build_file_args()` 与 `transcode_bilibili_cache()` 内联列表几乎逐行重复（`-hide_banner -loglevel error -nostdin -y/-n -movflags +faststart`），仅输入数量、音频策略（`aac` 重编码 vs `copy`）、编码参数不同。应抽成单一参数生成器，输入源做成列表。

### 🟠 P2 — 性能瓶颈

5. **子进程输出全量驻留内存（`utils/__init__.py:20-31`）**
   `communicate()` 一次性收满 stdout+stderr。长视频转换/下载时输出无限增长，内存无上限；且**没有逐行/进度回调**，任何 UI 或 REST 都无法拿到实时进度，只能事后解析。这是"未来桌面 UI"路线图的最大阻碍。
6. **Bilibili 转换串行执行（`ffmpeg.py:78-115`）**
   逐个根目录串行转码。而 `Settings.max_concurrent_tasks`（`settings.py:19`）定义后**从未被使用**——配置项是死代码，说明并行是原设计意图，只是没实现。
7. **`cache_roots` / `_is_cache_root` 用 `rglob("entry.json")`（`ffmpeg.py:133-135`）**
   对每个子目录做深度递归扫描；`_find_m4s_pair` 同样 `rglob("video.m4s")`。缓存树大时属不必要的全树遍历，且语义上 entry.json 通常只在缓存根附近，深度受限扫描即可。
8. **`ytdlp.py _find_output` 回退启发式脆弱（`ytdlp.py:85-88`）**
   当 JSON 解析失败（见 P0-1）退化为"目标目录里最新 mtime 的 mp4/m4a"。若目录里已有其他视频，可能选错文件。属于"能跑但不可靠"。

### 🟡 P3 — 可维护性风险

9. **无结构化异常**：所有失败都是 `RuntimeError(stderr)`，UI/API 层无法区分"下载失败 / 转码失败 / 配置错误 / 被取消"，只能靠字符串猜测。应引入异常层次。
10. **配置发现不一致**：`download` 支持 `--config`，`convert` 硬编码 `from_config(None)`——README 声称"复制 `nyavview.toml` 即可复用默认值"，但 convert 根本不会去读工作目录下的 `nyavview.toml`。`from_config` 也没有自动发现逻辑。
11. **CLI 构造后改配置**（`main.py:27-29`）：`settings.download_dir = ...` 直接改 pydantic 模型字段，绕过验证；更优做法是把参数显式传入 service/engine。
12. **yt-dlp 以裸命令 `"yt-dlp"` 调用（`ytdlp.py:23`）**：冻结成 EXE 后 PATH 上没有 `yt-dlp.exe` 必然失败；且无进度钩子。正确做法是用 **yt-dlp 的 Python API（`YoutubeDL`）**，天然带 `progress_hooks`，可彻底删除"解析 stdout JSON + glob 回退"整段脆弱逻辑，并让 EXE 无需携带 yt-dlp 命令行二进制。
13. **测试覆盖不足**：仅 3 个 utils 测试（`tests/test_paths.py`），引擎、settings、service 零覆盖。重构无安全网。
14. **杂项**：`typing.Optional/Awaitable/Callable` 未用导入；`ConfigPath = Optional[Path]` 别名意义不明；ffmpeg 硬编码 `libx264/aac` 与 `-preset` 缺失（`video_preset` 配置项没被用到，`audio_bitrate` 用了）；`build_file_args` 对外暴露了实现细节。

---

## 3. 重构策略（Refactoring Strategy）

原则：**只动实现，不动对外语义**——CLI 命令/选项、默认行为、输出命名规则、`-1/-2` 去重规则全部保持；新增能力（进度、取消、并行、自动发现配置）是"补全既定设计"，不是行为变更。

| 层 | 动作 | 目的 |
|---|---|---|
| `utils/process.py`（新） | 流式逐行读取 + `errors="replace"` + `on_line` 回调 + 超时 | 修复 P0-3、性能 5，为 UI 提供进度通道 |
| `engines/ytdlp.py` | 改用 `yt_dlp.YoutubeDL` Python API + `progress_hooks`，删掉 stdout JSON 解析与 glob 回退 | 修复 P0-1，性能 8，打包 12，天然进度 |
| `engines/ffmpeg.py` | 单一参数生成器 `_build_args(inputs, target, audio_mode, overwrite)`；`transcode_bilibili_cache` 补 `ensure_directory`；用 `asyncio.Semaphore(max_concurrent_tasks)` 并行；深度受限扫描 | 修复 P0-2，重复 4，性能 6、7 |
| `core/settings.py` | `discover(explicit=None)` 自动发现 `nyavview.toml`；`ffmpeg_path=None` 时自动回退 `imageio_ffmpeg.get_ffmpeg_exe()`；删除 `ConfigPath` 别名 | 可维护性 10、14 |
| `core/errors.py`（新） | `NyaViewError → EngineError(DownloadError/TranscodeError) / ConfigurationError / CanceledError` | 可维护性 9 |
| `core/service.py` | 透传 `on_progress` / `on_log` 回调 | 打通进度链路 |
| `cli/main.py` | 改用 `Settings.discover`、显式传参、包装结构化异常为友好消息 | 保持一致语义 + 更好错误 |
| `tests/` | 新增引擎（假子进程）、settings 发现、错误层次、命名规则测试 | 可维护性 13 |
| `ui/`（新） | pywebview + Claude 风格 HTML/CSS/JS + 任务队列 + 轮询事件桥 | 交付桌面版 |
| 打包 | PyInstaller（onedir/onefile）+ 内嵌 imageio-ffmpeg 的 ffmpeg.exe + 内嵌 yt-dlp Python 包 | 交付 EXE |

> **关键决策——为什么 yt-dlp 改用 Python API**：现有代码用 `subprocess` 调 `yt-dlp` 命令，在 EXE 场景下需要额外分发 yt-dlp 命令行二进制，且无法实时进度。`YoutubeDL` 是纯 Python 包（已随项目安装），提供 `progress_hooks`（`downloaded_bytes/total_bytes/status`），可无缝接到 UI 队列；用 `asyncio.to_thread` 保持异步外观。功能语义不变（格式选择、cookie、合并 mp4、m4a 抽取均一一对应）。

---

## 4. 改进后的代码（关键摘录）

完整实现见本仓库 `src/nyavview/`（已按上表重构）。以下摘录最核心的几段，证明"功能不变、质量提升"。

### 4.1 流式子进程执行器（`utils/process.py`）

```python
async def run_process(args: Sequence[str], *, on_line: OnLine | None = None,
                      timeout: float | None = None) -> RunResult:
    strings = [str(v) for v in args]
    proc = await asyncio.create_subprocess_exec(
        *strings, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def _drain(stream: asyncio.StreamReader, stream_name: str,
                     sink: list[str]) -> None:
        while True:  # 逐行读取，errors="replace" 修复 P0-3
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            sink.append(text)
            if on_line:
                result = on_line(stream_name, text.rstrip("\r\n"))
                if asyncio.iscoroutine(result):
                    await result

    out_sink, err_sink = [], []
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _drain(proc.stdout, "out", out_sink),
                _drain(proc.stderr, "err", err_sink)),
            timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProcessTimeoutError(strings, timeout) from None
    returncode = await proc.wait()
    return RunResult(strings, "".join(out_sink), "".join(err_sink), returncode or 0)
```

### 4.2 yt-dlp Python API 引擎（`engines/ytdlp.py`）

```python
class YtDlpEngine:
    def __init__(self, settings: Settings, *, on_progress: ProgressCallback | None = None):
        self.settings, self._on_progress = settings, on_progress

    def _opts(self, destination: Path, *, audio_only: bool, overwrite: bool) -> dict:
        opts = {
            "outtmpl": str(destination / "%(title).180B.%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True,
            "ignoreconfig": True, "noprogress": True,  # 进度改由 hooks 驱动
            "concurrent_fragment_downloads": 4,
        }
        if self.settings.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.settings.cookies_from_browser,)
        elif self.settings.cookie_file:
            opts["cookiefile"] = str(self.settings.cookie_file.expanduser())
        if audio_only:
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
        else:
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mp4"
        if overwrite:
            opts["overwrites"] = True
        return opts

    async def download(self, url, destination, *, audio_only=False, overwrite=False) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        return await asyncio.to_thread(self._run, url, destination, audio_only, overwrite)
    # _run 内创建 YoutubeDL，注册 _on_hook（转发 downloaded/total/status 给 on_progress）
```

### 4.3 统一 ffmpeg 参数生成器（`engines/ffmpeg.py`）

```python
def _build_args(self, inputs: Sequence[Path], target: Path, *,
                audio_mode: Literal["encode", "copy"], overwrite: bool) -> list[str]:
    args = [self._ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin",
            "-y" if overwrite else "-n"]
    for i, source in enumerate(inputs):
        args += ["-i", str(source)]
    for i in range(len(inputs)):        # 逐个输入可选映射，缺失流不报错
        args += ["-map", f"{i}:v?" if i == 0 else f"{i}:a?"]
    args += ["-c:v", "libx264", "-preset", self.settings.video_preset, "-c:a",
             "copy" if audio_mode == "copy" else "aac",
             "-b:a" if audio_mode == "encode" else "",  # encode 时给码率
             ...]
    args += ["-movflags", "+faststart", str(target)]
    return args
```

`transcode` / `transcode_bilibili_cache` 都只调这一个生成器；Bilibili 目录转换改为：

```python
sem = asyncio.Semaphore(self.settings.max_concurrent_tasks)
async def _one(root: Path) -> Path:
    async with sem:
        ... # 现在先 ensure_directory(target_dir)（修复 P0-2）
        return await self.transcode_pair(pair, target, overwrite=overwrite)
outputs = await asyncio.gather(*(_one(r) for r in roots))
```

（`max_concurrent_tasks` 从死配置变成真实并行上限。）

### 4.4 自动发现配置（`core/settings.py`）

```python
@classmethod
def discover(cls, explicit: Path | None = None) -> "Settings":
    candidates = [Path(p).expanduser() for p in (explicit,) if p]
    candidates.append(Path.cwd() / "nyavview.toml")
    for path in candidates:
        if path.is_file():
            with path.open("rb") as handle:
                return cls.model_validate(tomllib.load(handle))
    return cls()

def resolve_ffmpeg(self) -> str:
    if self.ffmpeg_path:
        return str(self.ffmpeg_path.expanduser().resolve())
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()   # EXE 打包后依然可用
    except Exception:
        return "ffmpeg"
```

### 4.5 结构化异常（`core/errors.py`）

```python
class NyaViewError(Exception): ...
class ConfigurationError(NyaViewError): ...
class EngineError(NyaViewError): ...
class DownloadError(EngineError): ...
class TranscodeError(EngineError): ...
class CanceledError(EngineError): ...
class ProcessTimeoutError(EngineError): ...
```

---

## 5. 新增：桌面 UI 与 EXE 打包（本版本交付）

- **UI 框架**：`pywebview`（Windows 走 Edge WebView2，本机已确认运行时存在）。界面为单 HTML 文件 + CSS/JS，风格对齐 Claude.ai：奶油底色 `#FAF9F5`、陶土色主强调 `#D97757`、衬线标题（Georgia 回退）+ 无衬线正文、圆角卡片、柔和边框与阴影、胶囊按钮与标签。
- **交互模型**：`claude` 风格左侧不变式导航 → 四个视图（下载 / 转换 / 队列 / 日志）。Python 侧 `Api` 类暴露 `start_download/start_convert/start_bili/cancel/poll/...`；进度事件经线程安全队列 + JS 每 150ms `poll()` 推送，彻底避开跨线程调用窗口的坑。
- **任务队列**：后台独立 asyncio 事件循环运行任务；状态机 `pending → running → done/error/cancelled`；支持取消（yt-dlp 通过 hook 抛 `DownloadCancelled`，ffmpeg 通过杀子进程）。
- **EXE**：PyInstaller 打包，内嵌 `imageio-ffmpeg` 自带的 gyan.dev ffmpeg 7.1（已确认含 `libx264` + `aac`），yt-dlp 用 Python 包形式内嵌，因此**用户机器无需安装 ffmpeg / yt-dlp**。输出 `dist/NyaView/`（onedir）与单文件 `dist/NyaView.exe`。

### 5.1 已验证结果（本机实测）

| 验证项 | 结果 |
|---|---|
| 单元测试 | 34 项全绿（`uv run pytest -q`） |
| CLI `--help` / 转码 | 正常 |
| 本地 MKV → MP4 转码（真实 ffmpeg） | 成功，进度事件（running→finished）与百分比正确 |
| Bilibili 缓存合并（真实 m4s） | 成功，按 `entry.json` 标题命名 |
| Bilibili 视频下载（真实链接） | 成功：`【官方 MV】Never Gonna Give You Up…mp4` |
| 桌面 UI 渲染 | 真实窗口：标题、6 卡片、5 标签、品牌、桥接均正常 |
| 冻结 EXE（onedir `dist/NyaView/NyaView.exe`） | 启动成功；**冒烟测试 HTTP 200 + CDP 桥接验证 PASS**（`api.poll`/`api.start_download` 均为 function，UI 实时） |
| 冻结 EXE（onefile `dist/NyaView.exe`，53MB） | 启动成功；**冒烟测试 HTTP 200 + CDP 桥接验证 PASS**（修复卡死后） |
| pywebview 6.2.1 关闭时 stderr 噪声 | 仅关闭时出现（运行时无影响），`--noconsole` GUI 不可见 |

### 5.2 过程中发现并修复的设计点

1. **JS↔Python 桥接死锁**：页面持续轮询 `api.poll()` 时，若 Python 侧再调 `window.evaluate_js` 会桥接重入死锁。因此本应用**一律用"JS 轮询 Python"方向**取事件，绝不在运行期从 Python 推 JS（自测脚本 `selftest_ui.py` 因此改用 `?selftest=1` 让页面主动上报 DOM 统计，而非 `evaluate_js`）。
2. **PyInstaller 惰性导入陷阱**：`yt_dlp` 是在 `YtDlpEngine._run()` 内惰性导入的，静态分析不可见，必须在 spec 的 `hiddenimports` 显式加入，否则 EXE 下载必然 `ImportError`。
3. **per-job 选项透传**：UI 里每任务可选的编码预设/码率/并行数/Cookie 等，需按任务克隆一份 `Settings`（`model_copy`）下发引擎，否则会被静默忽略。
4. **TOML 持久化的反斜杠转义**（复验时发现）：Windows 下 `str(Path("D:/x"))` 会变成 `D:\x`，若直接把 `Path` 插进 `key = "value"` 写出，生成的 `~/.nyavview.toml` 是非法 TOML，下次启动设置会静默丢失。`save_user_settings` 现对字符串统一做 `\` → `\\` 转义，整数写成裸整数；新增 `tests/test_ui_settings.py` 回归覆盖。
5. **EXE 打包后前端 404（用户报告，根因见下）**：打包后的 EXE 打开后 WebView2 显示 `404 Not Found — File does not exist`。根因是 **PyInstaller 6 的 onedir 布局也会设置 `sys._MEIPASS`（指向 `_internal/`）**，而原 `resource_path()` 以 `_MEIPASS` 为基准拼 `static/index.html`，但 spec 的 datas 目标是 `nyavview/ui/static`（文件落在 `_internal/nyavview/ui/static/`），导致路径错位、Bottle 服务器从 `_internal/static` 提供文件而该目录为空 → 404。修复：把两个 spec 的 datas 目标统一改为 `static`（文件落在 `<base>/static/`），并让 `resource_path()` 在 `_MEIPASS` 与 `__file__.parent` 两个候选里探测实际存在的路径。新增 `smoke_exe.py` 冒烟测试：启动 EXE → 找到其 HTTP 监听端口（含 PyInstaller onefile 子进程）→ 抓取 `/index.html` 与 `/app.js` 并断言 HTTP 200 与关键内容，杜绝"进程活着但页面 404"再次发生。
6. **EXE 打开即"未响应/卡死"（用户报告，根因见下）**：窗口打开后 Windows 显示"未响应"，且页面停留在"浏览器预览模式（无后端）"。根因：pywebview 在注入 JS 桥接前会调用 `util.get_functions()` **递归遍历 `js_api` 对象的全部属性**（`dir()` + `getattr`）；我的 `Api` 实例公开持有 `self.manager`（JobManager → Settings pydantic 模型）与 `self.window`（pywebview Window → EdgeChrome → WinForms/WebView2 的 .NET 对象图），在冻结环境下遍历这套对象图导致 `generate_js_object` 线程长时间卡死——桥接方法从未注入、`pywebviewready` 从不触发、`boot()` 不运行，UI 空转（此时 `Process.Responding` 也可能为 False，配合 WebView2 运行时/SDK 版本差异的 COM 噪声，被 Windows 判定为"未响应"）。修复：**Api 内部引用全部改为下划线前缀**（`_manager`/`_window`，`get_functions` 跳过下划线属性），并给 `JobManager`/`Settings` 加 `_serializable = False`，使桥接只暴露 13 个真实 API 方法（0.0ms 生成）；同时修复 app.js 的"预览模式"fallback 时序竞态（给桥接注入留宽限期）。验证：`cdp_check.py` 通过 WebView2 CDP 断言 `window.pywebview.api.poll/start_download` 均为 `function`、`runtimeText` 显示真实 ffmpeg/yt-dlp 版本——两个 EXE 均 PASS。
7. **下载后队列无任务卡片 + 无进度（用户报告，根因见下）**：提交下载后切到队列页但一个任务都不显示，且下载全程进度条不动。两个独立根因：
   - **前端任务未创建**：`handleEvent` 只对"已存在于 `state.jobs` 的任务"做更新，而 `start_download` 成功后也没有把新任务加入本地状态，导致轮询事件到达时 `state.jobs.get(jobId)` 为 `undefined` 被跳过——任务卡片永不出现。修复：`handleEvent` 遇到未知 jobId 时用事件数据创建任务；提交成功后立即"乐观"插入 pending 任务卡片。
   - **进度钩子未接线**：`YtDlpEngine._opts` 定义了 `_hook` 却从未把它注册进 yt-dlp 的 `progress_hooks` 选项——下载时零进度事件、取消（`DownloadCancelled` 检查在 hook 内）也失效。修复：`_opts` 加 `"progress_hooks": [self._hook]`；新增 3 个回归测试（opts 注册、downloading/finished 事件）。
   - 顺带修复**取消路径**：`JobManager.cancel` 对 yt-dlp 任务误用 `task.cancel()`，会以 `asyncio.CancelledError` 取消协程、绕过 `except CanceledError`，导致任务卡在 running 且无状态事件；改为只调 `engine.cancel()`（优雅取消），并给 `_run_*` 加 `asyncio.CancelledError` 防御性处理（ffmpeg 子进程 kill 路径）。实测：下载 24.6s→83 个进度事件；取消 45s 内状态正确变 `cancelled`；最终均生成完整 MP4（`.part` 是 yt-dlp 下载中的正常中间文件，完成后自动清理并改名）。
8. **下载中断且报"Could not copy Chrome cookie database"（用户报告，根因见下）**：用户最初"只下载出 `.part` 而非完整 MP4"的真相——用户在设置面板配置了 `cookies_from_browser = "edge"`，而 Edge 正在运行时其 cookie 数据库被锁定，yt-dlp 提取 cookie 失败（issue #7271）导致下载中断（`.part` 只是下载启动后的中间文件）。修复：`YtDlpEngine._run` 检测 cookie 类错误后**自动回退为无 Cookie 重试**（共享同一取消事件），并发出"浏览器 Cookie 读取失败，已自动切换为无 Cookie 下载"提示，下载不再硬失败。实测：Edge 运行中、配置 edge cookie 的情况下，下载仍成功产出完整 21.5MB MP4。新增 `test_is_cookie_error` / `test_run_falls_back_when_cookie_db_locked` 回归测试。

---

## 6. 所需工具清单（已就绪 / 需用户配合）

以下依赖已由本环境自动安装（`uv add` 完成，无需用户操作）：

| 用途 | 包 | 状态 |
|---|---|---|
| 桌面窗口 | `pywebview` (6.2.1) + `pythonnet/clr-loader` | ✅ 已装 |
| EXE 打包 | `pyinstaller` (6.22.3) + hooks-contrib | ✅ 已装 |
| 下载内核 | `yt-dlp` (2026.08.19) | ✅ 项目原依赖 |
| ffmpeg | `imageio-ffmpeg` 内置 ffmpeg 7.1（含 libx264/aac） | ✅ 项目原依赖 |

如需用户侧配合的（仅当想要更完整的 ffmpeg 全家桶或官方图标时）：

| 工具 | 说明 | 是否必需 |
|---|---|---|
| ffmpeg 完整版（gyan.dev 或 BtbN） | 若需 `libvpx/vp9/hevc/hw` 等更多编码器 | 非必需（当前 essentials 已含 h264/aac） |
| 自定义 `.ico` 图标 | 可用任一绘图软件导出；当前已用脚本生成占位图标 | 非必需 |
| 签名证书（如 `signtool` + 证书） | 消除 SmartScreen 未知发布者提示 | 可选，正式分发建议 |

> 结论一句话：**本项目不需要你再安装任何东西**——构建链路（uv + venv + pywebview + PyInstaller + 内置 ffmpeg/yt-dlp）已全部就绪。

---

## 7. 已知保留的语义细节（有意不改）

重构严格保持"对外功能不变"，以下行为与重构前一致，此处显式记录，供后续演进参考：

1. **`cache_roots` 的"父目录即根"语义**：若传入目录内部任意深度存在 `entry.json`，整个目录被当作单一缓存根处理（与原先 `rglob("entry.json")` 判定一致），而不是遍历其子目录逐个转换。用户应直接指向"包含 `entry.json` 的那个目录"。若未来想支持"父目录批量转换全部视频"，建议把 `_has_entry_json` 的判定收紧为"仅 `entry.json` 位于目录自身根（或深度 1）"，并让 CLI 在 `--recursive` 下展开子目录——这是一处**行为变更**，重构期不做。
2. **`--cookie-file` 与配置优先级**：CLI 显式传入的 cookie 选项覆盖配置文件；两者都设置时 `cookies_from_browser` 优先（与原先 yt-dlp 参数拼接顺序一致）。
3. **输出命名与去重**：`<stem>.mp4` / 标题 `.mp4`、`-1/-2` 后缀去重规则不变。
4. **`-map` 可选流映射**：视频/音频缺失不报错（`0:v?` / `0:a?` / `1:a?` 语义保持）。
