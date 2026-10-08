# NyaView

异步视频下载与 MP4 转码工具：既有命令行版本，也附带 **Claude 风格桌面 UI**（Windows EXE）。

- 下载：粘贴链接即下（Bilibili / YouTube / yt-dlp 支持的所有站点），可选仅音频、Cookie、登录画质。
- 转换：本地任意格式视频重编码为兼容性最好的 H.264 + AAC MP4。
- 合并：Bilibili 客户端缓存（`entry.json + video.m4s + audio.m4s`）合并为单个 MP4。

架构分层（`engines` / `core` / `utils` / `cli` / `ui`），同一套 core 同时驱动 CLI 与桌面 UI。
工程评审与重构说明见 [docs/engineering_review.md](docs/engineering_review.md)。

## 运行（开发环境）

```sh
uv sync
uv run pytest -q          # 测试
uv run nyavview --help    # CLI
uv run nyavview-gui       # 桌面 UI
```

## 命令行用法

```sh
nyavview download <url> --destination <folder>
nyavview convert <local-video-file> --destination <folder>
nyavview convert <bilibili-cache-folder> --destination <folder>
```

Cookie 支持：

```sh
nyavview download <url> --cookies-from-browser edge
nyavview download <url> --cookie-file cookies.txt
```

配置：复制 `nyavview.toml.example` 为 `nyavview.toml`（放当前目录）即可作为默认配置；
桌面 UI 的设置保存在 `~/.nyavview.toml`，两者同构。

## 桌面 UI（Windows EXE）

预构建产物位于 `dist/NyaView/`（免安装目录版，双击 `NyaView.exe` 即可）：

- 五视图：下载 / 转换 / 队列 / 日志 / 设置
- 实时进度（下载速度 / ETA / 转码百分比）、任务取消、完成后一键查看输出
- 内置 ffmpeg（imageio-ffmpeg 自带的 gyan.dev build）与 yt-dlp（Python 内核），**无需在系统安装 ffmpeg / yt-dlp**
- 界面风格对齐 Claude.ai：奶油底色、陶土色强调、衬线标题、圆角卡片

重新打包：

```sh
uv run pyinstaller --noconfirm --clean nyavview.spec
```

> 需要 WebView2 运行时（Win10/11 通常已内置；旧系统可在 Microsoft 官网安装 Evergreen Runtime）。

## 开发

```sh
uv run pytest -q
uv run nyavview --help
uv run python selftest_ui.py   # 打开真实窗口自检 UI 渲染
```
