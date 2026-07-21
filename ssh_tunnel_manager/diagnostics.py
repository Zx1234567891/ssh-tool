from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import zipfile

from . import __version__
from .health import HealthSnapshot
from .models import AppState
from .store import StateStore


def _sanitized_config(state: AppState) -> dict:
    payload = state.to_dict()
    settings = payload.get("settings", {})
    if isinstance(settings, dict):
        if settings.get("clash_controller_secret"):
            settings["clash_controller_secret"] = "<redacted>"
    return payload


def create_diagnostic_bundle(
    store: StateStore,
    state: AppState,
    snapshots: dict[str, HealthSnapshot],
) -> Path:
    store.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = store.diagnostics_dir / f"SshTunnelManager-Diagnostics-{stamp}.zip"
    system = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "app_version": __version__,
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "config_schema": store.loaded_schema_version,
        "config_read_only": store.read_only,
    }
    health = {host_id: snapshot.to_dict() for host_id, snapshot in snapshots.items()}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("system.json", json.dumps(system, ensure_ascii=False, indent=2))
        archive.writestr(
            "config-sanitized.json",
            json.dumps(_sanitized_config(state), ensure_ascii=False, indent=2),
        )
        archive.writestr("health.json", json.dumps(health, ensure_ascii=False, indent=2))
        for pattern in ("app.log*", "events.jsonl*"):
            for path in sorted(store.log_dir.glob(pattern)):
                if path.is_file():
                    archive.write(path, f"logs/{path.name}")
    return target
