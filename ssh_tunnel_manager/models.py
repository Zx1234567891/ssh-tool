from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import os


def default_ssh_path() -> str:
    windows = os.environ.get("WINDIR", r"C:\Windows")
    candidate = Path(windows) / "System32" / "OpenSSH" / "ssh.exe"
    return str(candidate)


def default_ssh_config_path() -> str:
    return str(Path.home() / ".ssh" / "config")


@dataclass(slots=True)
class AppSettings:
    ssh_path: str = field(default_factory=default_ssh_path)
    ssh_config_path: str = field(default_factory=default_ssh_config_path)
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

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppSettings":
        data = data or {}
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class HostConfig:
    alias: str
    display_name: str = ""
    enabled: bool = False
    remote_proxy_port: int = 10099
    remote_dir: str = "~"
    auto_reconnect: bool = True

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.alias

    @classmethod
    def from_dict(cls, data: dict) -> "HostConfig":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict:
        return asdict(self)


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

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppState":
        data = data or {}
        return cls(
            settings=AppSettings.from_dict(data.get("settings")),
            hosts=[HostConfig.from_dict(item) for item in data.get("hosts", [])],
        )

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "settings": self.settings.to_dict(),
            "hosts": [host.to_dict() for host in self.hosts],
        }
