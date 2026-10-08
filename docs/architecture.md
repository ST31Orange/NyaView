# Architecture

The project is deliberately split into a few focused layers so the current CLI and any future UI-FastAPI combination can reuse them without rework.

Layer map:

- `core` - user settings, shared models, and option objects. Nothing in `core` knows about the CLI.
- `core.service` - a tiny facade exposing download/convert operations to CLI, desktop UI, or REST without duplicated logic.
- `engines` - one thin adapter per external tool: `yt-dlp` and `ffmpeg`. Engines do not care about settings storage or CLI surfaces.
- `cli` - user-facing argument parsing. This is the first frontend. Later, REST endpoints or a native UI can call the same public functions and data models.
- `plugins` - intentionally left open for Bilibili-specific helpers and future site integrations.
- `utils` - subprocess wrapper, path helpers, and small generic utilities.

The engines are async-ready, which is the key constraint for the future desktop UI and REST API. They call subprocesses in async contexts, so the rest of the app can later be driven by FastAPI, a worker queue, or a desktop UI without redesigning the internals.
