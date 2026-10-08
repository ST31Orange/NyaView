# NyaView

Async video downloader and MP4 transcoder. This first version is a command-line tool designed for desktop use. The architecture is prepared so a later desktop UI or REST frontend can drive the same core without rework.

## Features

- Bilibili cache folder detection and MP4 conversion.
- Local video and audio transcode into a friendly MP4.
- Optional logged-in Bilibili quality via cookie file or browser cookies.
- Local output directory control and explicit overwrite handling.
- Plugin-ready URL handling for future site additions.

## Install

```sh
uv sync
```

## Usage

```sh
nyavview download <url> --destination <folder>
nyavview convert <local-video-file> --destination <folder>
nyavview convert <bilibili-cache-folder> --destination <folder>
```

Optional cookie support:

```sh
nyavview download <url> --cookies-from-browser edge
nyavview download <url> --cookie-file cookies.txt
```

Copy `nyavview.toml.example` to `nyavview.toml` to configure the defaults once and reuse them from the CLI.

## Architecture

The code is intentionally thin and layered:

- `engines` - direct adapters for `yt-dlp` and `ffmpeg`.
- `core` - settings and task-related abstractions.
- `plugins` - future Bilibili or site-specific helpers.
- `utils` - small process, path, and formatting helpers.
- `cli` - the first user-facing surface; later, a desktop UI or REST endpoints can use the same engines.

See `docs/architecture.md` for more detail.

## Development

```sh
uv run pytest -q
uv run nyavview --help
```
