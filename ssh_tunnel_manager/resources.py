from __future__ import annotations

from pathlib import Path
import sys


def resource_path(relative: str) -> Path:
    """Resolve an asset in source runs and PyInstaller one-file builds."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / relative
    return Path(__file__).resolve().parent.parent / relative
