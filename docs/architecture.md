# Architecture

分层目标：CLI、桌面 UI、未来的 REST 共用同一套 core，不重复逻辑。

Layer map:

- `core` - 用户设置（`Settings`）、共享事件模型（`ProgressEvent`）、结构化异常（`errors`）。
- `core.service` - 薄门面，暴露 download / convert / bilibili 操作，带可选进度回调。
- `engines` - 每个外部工具一个薄适配器：`yt-dlp`（Python API）与 `ffmpeg`（子进程）。
  引擎不关心设置存储或前端形态。
- `cli` - typer 命令行前端。
- `ui` - pywebview 桌面前端：
  - `ui/app.py` - 窗口入口（含 PyInstaller 资源路径处理）；
  - `ui/api.py` - JS 桥（任务提交、文件对话框、设置持久化到 `~/.nyavview.toml`）；
  - `ui/queue.py` - 后台 asyncio 任务队列（状态机、进度事件队列、取消）；
  - `ui/static/` - Claude 风格前端（单页 HTML/CSS/JS）。
- `utils` - 流式子进程封装（`process`）、路径助手（`paths`）、文件名净化（`__init__`）。

## 数据流

```
CLI / UI  ──►  core.service ──►  engines（yt-dlp / ffmpeg）
                                  │  进度事件（ProgressEvent）
                                  ▼
                     ui.queue.JobManager（线程安全事件队列）
                                  ▲
                     UI 前端每 150ms api.poll() 拉取
```

关键约束：

- 引擎全部 async-ready（asyncio 子进程 / `asyncio.to_thread` 包住阻塞的 YoutubeDL），
  CLI 用 `asyncio.run`，UI 用独立后台事件循环线程驱动。
- 引擎通过 `on_progress` 回调发射 `ProgressEvent`，UI 通过"JS 轮询 Python"方向取事件——
  刻意避免 Python→JS 的 `evaluate_js` 推送（页面在轮询桥时调用 `evaluate_js` 会桥接重入死锁，
  见 `selftest_ui.py` 的注释）。
- 冻结为 EXE 时：ffmpeg 来自 `imageio-ffmpeg` 自带的静态构建（含 libx264/aac），
  yt-dlp 以 Python 包形式内嵌（`YtDlpEngine` 惰性导入，PyInstaller spec 显式 hidden import）。

详见 [engineering_review.md](engineering_review.md)（工程评审、问题清单、重构策略）。
