"""PyInstaller launcher for the NyaView desktop app."""

import sys
from pathlib import Path

# Make bundled (frozen) resources importable when running unpackaged too.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from nyavview.ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
