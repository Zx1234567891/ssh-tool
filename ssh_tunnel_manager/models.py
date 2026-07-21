from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import uuid


SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _known_payload(instance, excluded: set[str] | None = None) -> dict:
    excluded = (excluded or set()) | {"extra"}
    payload = dict(getattr(instance, "extra", {}))
    for name in instance.__dataclass_fields__:
        if name not in excluded:
            payload[name] = getattr(instance, name)
    return payload


def default_ssh_path() -> str:
    windows = os.environ.get("WINDIR", r"C:\Windows")
    candidate = Path(windows) / "System32" / "OpenSSH" / "ssh.exe"
    return str(candidate)


def default_ssh_config_path() -> str:
    return str(Path.home() / ".ssh" / "config")


def find_vscode_path() -> str:
    """Locate Code.exe even when only code.cmd is exposed on PATH."""
    candidates: list[Path] = []
    command = shutil.which("code") or shutil.which("code.cmd")
    if command:
        command_path = Path(command)
        candidates.append(command_path)
        if command_path.suffix.lower() in {".cmd", ".bat"} and command_path.parent.name.lower() == "bin":
            candidates.insert(0, command_path.parent.parent / "Code.exe")

    local = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if local:
        candidates.extend([
            Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe",
            Path(local) / "Programs" / "Microsoft VS Code Insiders" / "Code - Insiders.exe",
        ])
    if program_files:
        candidates.append(Path(program_files) / "Microsoft VS Code" / "Code.exe")
    if program_files_x86:
        candidates.append(Path(program_files_x86) / "Microsoft VS Code" / "Code.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


@dataclass(slots=True)
class AppSettings:
    ssh_path: str = field(default_factory=default_ssh_path)
    ssh_config_path: str = field(default_factory=default_ssh_config_path)
    vscode_path: str = field(default_factory=find_vscode_path)
    local_proxy_host: str = "127.0.0.1"
    local_proxy_port: int = 7892
    default_remote_proxy_port: int = 10099
    keepalive_interval: int = 30
    keepalive_count_max: int = 3
    connect_timeout: int = 10
    smoke_timeout: int = 35
    proxy_test_url: str = (
        "https://update.code.visualstudio.com/"
        "commit:8a7abeba6e03ea3af87bfbce9a1b7e48fed567b8/"
        "cli-alpine-x64/stable"
    )
    minimize_to_tray: bool = True
    start_enabled_on_launch: bool = False
    log_level: str = "INFO"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5
    log_retention_days: int = 14
    health_probe_interval: int = 60
    openai_test_url: str = "https://chatgpt.com/"
    codex_log_level: str = "info"
    ssh_debug_logging: bool = False
    clash_controller_url: str = ""
    clash_controller_secret: str = ""
    check_updates_on_launch: bool = True
    update_repository: str = "Zx1234567891/ssh-tool"
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppSettings":
        data = data or {}
        allowed = set(cls.__dataclass_fields__) - {"extra"}
        values = {key: value for key, value in data.items() if key in allowed}
        values["extra"] = {key: value for key, value in data.items() if key not in allowed}
        return cls(**values)

    def to_dict(self) -> dict:
        return _known_payload(self)


@dataclass(slots=True)
class HostConfig:
    alias: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    display_name: str = ""
    enabled: bool = False
    remote_proxy_port: int = 10099
    remote_dir: str = "~"
    workspaces: list[str] = field(default_factory=list)
    auto_reconnect: bool = True
    source: str = "user_created"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    extra: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.display_name:
            self.display_name = self.alias
        if not isinstance(self.workspaces, list):
            self.workspaces = []
        self.workspaces = self._unique_paths(self.workspaces)

    @staticmethod
    def _unique_paths(paths: list[str]) -> list[str]:
        unique: list[str] = []
        for value in paths:
            path = str(value).strip()
            if path and path not in unique:
                unique.append(path)
        return unique

    def workspace_shortcuts(self) -> list[str]:
        """Return the default workspace followed by recent VSCode locations."""
        return self._unique_paths([self.remote_dir, *self.workspaces])

    def remember_workspace(self, path: str, limit: int = 10) -> None:
        """Move a workspace to the front of this host's bounded history."""
        path = path.strip()
        if not path:
            return
        self.workspaces = [path, *[item for item in self.workspaces if item != path]][:limit]
        self.updated_at = utc_now()

    def forget_workspace(self, path: str) -> None:
        self.workspaces = [item for item in self.workspaces if item != path]
        self.updated_at = utc_now()

    @classmethod
    def from_dict(cls, data: dict) -> "HostConfig":
        allowed = set(cls.__dataclass_fields__) - {"extra"}
        values = {key: value for key, value in data.items() if key in allowed}
        values["extra"] = {key: value for key, value in data.items() if key not in allowed}
        return cls(**values)

    def to_dict(self) -> dict:
        return _known_payload(self)


def connected_hosts_first(
    hosts: list[HostConfig], connected_aliases: set[str]
) -> list[HostConfig]:
    """Return a stable display order with connected hosts at the top."""
    return sorted(hosts, key=lambda host: host.alias not in connected_aliases)


@dataclass(slots=True)
class ResolvedHost:
    alias: str
    hostname: str = ""
    user: str = ""
    port: int = 22
    identity_files: list[str] = field(default_factory=list)
    proxy_jump: str = ""
    configured_remote_forwards: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AppState:
    settings: AppSettings = field(default_factory=AppSettings)
    hosts: list[HostConfig] = field(default_factory=list)
    onboarding_completed: bool = False
    extra: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppState":
        data = data or {}
        known = {"schema_version", "version", "settings", "hosts", "onboarding_completed"}
        return cls(
            settings=AppSettings.from_dict(data.get("settings")),
            hosts=[HostConfig.from_dict(item) for item in data.get("hosts", [])],
            onboarding_completed=bool(data.get("onboarding_completed", data.get("hosts"))),
            extra={key: value for key, value in data.items() if key not in known},
        )

    def to_dict(self) -> dict:
        return {
            **self.extra,
            "schema_version": SCHEMA_VERSION,
            # Keep this legacy key for older builds that only know `version`.
            "version": SCHEMA_VERSION,
            "settings": self.settings.to_dict(),
            "hosts": [host.to_dict() for host in self.hosts],
            "onboarding_completed": self.onboarding_completed,
        }
