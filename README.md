# NyaView

Async video downloader and MP4 transcoder for desktop use.

## Install

```sh
uv sync
```

## Run

```sh
nyavview --help
nyavview download <url> --destination <folder>
nyavview convert <source> --destination <folder>
```

## Future UI development
The code is separated into:

- `engines` provide direct download/convert abilities.
- `plugins` abstract site-specific logic.
- `core` owns user options and task orchestration.
- `cli` is the first view of the core service.
- Later, another UI can call these engines via FastAPI or a native UI without touching the engines.
