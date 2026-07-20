from __future__ import annotations

from pathlib import Path
import glob
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime

from .models import ResolvedHost


@dataclass(slots=True)
class SshHostEntry:
    alias: str
    hostname: str
    user: str
    port: int = 22
    identity_file: str = ""
    proxy_jump: str = ""


def _validate_single_line(label: str, value: str) -> str:
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"{label}不能为空或包含换行")
    return value


def append_host_entry(config_path: str, entry: SshHostEntry) -> Path | None:
    """Append one literal Host block, preserving the existing file and a backup."""
    alias = _validate_single_line("SSH 别名", entry.alias)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", alias):
        raise ValueError("SSH 别名只能包含字母、数字、点、下划线和短横线")
    hostname = _validate_single_line("主机地址", entry.hostname)
    user = _validate_single_line("用户名", entry.user)
    if not 1 <= int(entry.port) <= 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")
    if "*" in alias or "?" in alias or "!" in alias:
        raise ValueError("新主机别名不能包含通配符")

    path = Path(config_path).expanduser()
    existing_aliases = parse_host_aliases(str(path)) if path.exists() else []
    if alias.casefold() in {item.casefold() for item in existing_aliases}:
        raise ValueError(f"SSH config 中已存在 Host {alias}")

    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8-sig", errors="replace")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.ssh-tunnel-manager-backup-{stamp}")
        shutil.copy2(path, backup)

    lines = [
        "# Added by SSH Tunnel Manager",
        f"Host {alias}",
        f"  HostName {hostname}",
        f"  User {user}",
        f"  Port {int(entry.port)}",
    ]
    if entry.identity_file.strip():
        identity = str(Path(entry.identity_file.strip()).expanduser()).replace("\\", "/").replace('"', '\\"')
        lines.append(f'  IdentityFile "{identity}"')
    if entry.proxy_jump.strip():
        jump = _validate_single_line("跳板机", entry.proxy_jump)
        lines.append(f"  ProxyJump {jump}")
    lines.extend(["  ServerAliveInterval 30", "  ServerAliveCountMax 3"])
    block = "\n".join(lines) + "\n"
    payload = existing
    if payload and not payload.endswith(("\n", "\r")):
        payload += "\n"
    if payload:
        payload += "\n"
    payload += block

    fd, temporary_name = tempfile.mkstemp(prefix="ssh-config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return backup


def _clean_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _read_config(path: Path, seen: set[Path]) -> list[str]:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved in seen or not path.is_file():
        return []
    seen.add(resolved)

    aliases: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return aliases

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "host":
            try:
                names = shlex.split(value, posix=False)
            except ValueError:
                names = value.split()
            for name in names:
                name = _clean_value(name)
                if name and not any(ch in name for ch in "*!?") and name not in aliases:
                    aliases.append(name)
        elif key == "include":
            try:
                patterns = shlex.split(value, posix=False)
            except ValueError:
                patterns = value.split()
            for pattern in patterns:
                include = Path(_clean_value(pattern)).expanduser()
                if not include.is_absolute():
                    include = path.parent / include
                for match in glob.glob(str(include)):
                    for alias in _read_config(Path(match), seen):
                        if alias not in aliases:
                            aliases.append(alias)
    return aliases


def parse_host_aliases(config_path: str) -> list[str]:
    return _read_config(Path(config_path).expanduser(), set())


def resolve_host(ssh_path: str, config_path: str, alias: str, timeout: int = 8) -> ResolvedHost:
    command = [ssh_path, "-F", config_path, "-G", alias]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"无法解析 SSH 主机 {alias}")

    values: dict[str, list[str]] = {}
    for raw in result.stdout.splitlines():
        parts = raw.split(None, 1)
        if len(parts) == 2:
            values.setdefault(parts[0].lower(), []).append(parts[1].strip())

    port_text = (values.get("port") or ["22"])[0]
    return ResolvedHost(
        alias=alias,
        hostname=(values.get("hostname") or [alias])[0],
        user=(values.get("user") or [""])[0],
        port=int(port_text) if port_text.isdigit() else 22,
        identity_files=values.get("identityfile", []),
        proxy_jump=(values.get("proxyjump") or [""])[0],
        configured_remote_forwards=values.get("remoteforward", []),
    )
