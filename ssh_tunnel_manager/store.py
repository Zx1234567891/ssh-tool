from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime

from .models import AppState, SCHEMA_VERSION, utc_now


def default_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "SshTunnelManager"
    return Path.home() / ".ssh-tunnel-manager"


def default_local_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "SshTunnelManager"
    return default_data_dir()


class UnsupportedConfigVersion(RuntimeError):
    pass


class StateStore:
    def __init__(self, data_dir: Path | None = None, local_data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.local_data_dir = local_data_dir or (self.data_dir if data_dir else default_local_data_dir())
        self.path = self.data_dir / "config.json"
        self.backup_dir = self.data_dir / "backups"
        self.log_dir = self.local_data_dir / "logs"
        self.diagnostics_dir = self.local_data_dir / "diagnostics"
        self.update_dir = self.local_data_dir / "updates"
        self.runtime_path = self.local_data_dir / "state.json"
        self.read_only = False
        self.loaded_schema_version = SCHEMA_VERSION

    def load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        version = self._schema_version(payload)
        self.loaded_schema_version = version
        if version > SCHEMA_VERSION:
            self.read_only = True
            return AppState.from_dict(payload)
        if version < SCHEMA_VERSION:
            payload = self._migrate(payload, version)
            self._write_payload(payload)
        return AppState.from_dict(payload)

    def save(self, state: AppState) -> None:
        if self.read_only:
            raise UnsupportedConfigVersion(
                f"配置版本 {self.loaded_schema_version} 高于当前支持的 {SCHEMA_VERSION}，已禁止覆盖保存"
            )
        self._write_payload(state.to_dict())

    @staticmethod
    def _schema_version(payload: dict) -> int:
        value = payload.get("schema_version", payload.get("version", 1))
        try:
            version = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无效的配置版本：{value!r}") from exc
        if version < 1:
            raise ValueError(f"无效的配置版本：{version}")
        return version

    def _migrate(self, original: dict, version: int) -> dict:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = self.backup_dir / f"config-v{version}-{stamp}.json"
        shutil.copy2(self.path, backup)
        payload = json.loads(json.dumps(original))
        while version < SCHEMA_VERSION:
            if version == 1:
                payload = self._migrate_v1_to_v2(payload)
            else:
                raise ValueError(f"没有从配置版本 {version} 开始的迁移程序")
            version += 1
        return payload

    @staticmethod
    def _migrate_v1_to_v2(payload: dict) -> dict:
        import uuid

        now = utc_now()
        hosts = payload.get("hosts") if isinstance(payload.get("hosts"), list) else []
        for host in hosts:
            if not isinstance(host, dict):
                continue
            host.setdefault("id", str(uuid.uuid4()))
            host.setdefault("source", "migration")
            host.setdefault("created_at", now)
            host.setdefault("updated_at", now)
        payload["hosts"] = hosts
        payload["onboarding_completed"] = bool(hosts)
        payload["migration_v2_review_pending"] = bool(hosts)
        payload["schema_version"] = 2
        payload["version"] = 2
        return payload

    def _write_payload(self, data: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
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

    def load_runtime(self) -> dict:
        if not self.runtime_path.exists():
            return {}
        try:
            return json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_runtime(self, data: dict) -> None:
        self.local_data_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(data)
        payload["updated_at"] = utc_now()
        fd, temporary_name = tempfile.mkstemp(
            prefix="state-", suffix=".tmp", dir=self.local_data_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary_name, self.runtime_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
