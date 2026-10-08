from pathlib import Path


def ensure_directory(path: Path) -> Path:
    """Create a directory and resolve it."""
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def unique_path(path: Path) -> Path:
    """Append -1, -2, ... if a target already exists."""
    if not path.exists():
        return path
    for value in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{value}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"too many files at {path}")
