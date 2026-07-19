from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .models import AppState


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "SshTunnelManager"
    return Path.home() / ".ssh-tunnel-manager"


class StateStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.path = self.data_dir / "config.json"
        self.log_dir = self.data_dir / "logs"

    def load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        with self.path.open("r", encoding="utf-8") as handle:
            return AppState.from_dict(json.load(handle))

    def save(self, state: AppState) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        fd, temporary_name = tempfile.mkstemp(
            prefix="config-", suffix=".tmp", dir=self.data_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
