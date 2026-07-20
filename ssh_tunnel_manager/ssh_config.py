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


PROXY_INCLUDE_NAME = "ssh-tunnel-manager-proxy.conf"


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

    _atomic_write(path, payload, "ssh-config-")
    return backup


def _atomic_write(path: Path, payload: str, prefix: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(temporary_name, path)
        _harden_openssh_permissions(path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _harden_openssh_permissions(path: Path) -> None:
    """Keep generated Windows SSH config files acceptable to OpenSSH."""
    if os.name != "nt":
        return
    identity = subprocess.run(
        ["whoami.exe"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    if not identity:
        raise RuntimeError("无法确定当前 Windows 用户，未能设置 SSH 配置权限")
    result = subprocess.run(
        [
            "icacls.exe", str(path), "/inheritance:r", "/grant:r",
            f"{identity}:(F)", "*S-1-5-18:(F)", "*S-1-5-32-544:(F)",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def upsert_proxy_setenv(config_path: str, alias: str, port: int) -> Path:
    """Store this PC's per-host proxy port in a generated SSH include file."""
    alias = _validate_single_line("SSH 别名", alias)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", alias):
        raise ValueError("SSH 别名只能包含字母、数字、点、下划线和短横线")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("远程代理端口必须在 1 到 65535 之间")

    path = Path(config_path).expanduser()
    include_path = path.with_name(PROXY_INCLUDE_NAME)
    include_line = f'Include "{include_path.resolve().as_posix()}"'
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else ""
    include_is_present = False
    for raw in existing.splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "include":
            continue
        try:
            patterns = shlex.split(parts[1], posix=False)
        except ValueError:
            patterns = parts[1].split()
        for pattern in patterns:
            candidate = Path(_clean_value(pattern)).expanduser()
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            try:
                if candidate.resolve() == include_path.resolve():
                    include_is_present = True
            except OSError:
                if str(candidate).casefold() == str(include_path).casefold():
                    include_is_present = True

    if not include_is_present:
        if path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = path.with_name(f"{path.name}.ssh-tunnel-manager-backup-{stamp}")
            shutil.copy2(path, backup)
        header = "# SSH Tunnel Manager: per-PC proxy ports\n" + include_line + "\n"
        payload = header + ("\n" + existing if existing else "")
        _atomic_write(path, payload, "ssh-config-")

    ports: dict[str, int] = {}
    if include_path.exists():
        current_alias = ""
        for raw in include_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            stripped = raw.strip()
            parts = stripped.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "host":
                names = parts[1].split()
                current_alias = names[0] if len(names) == 1 and names[0] != "*" else ""
                continue
            match = re.fullmatch(
                r"SetEnv\s+LC_STM_PROXY_PORT=([0-9]+)", stripped, flags=re.IGNORECASE
            )
            if current_alias and match:
                ports[current_alias] = int(match.group(1))
    ports[alias] = port

    lines = [
        "# Generated by SSH Tunnel Manager. Changes may be overwritten.",
        "# Each Windows PC keeps its own port values in this local file.",
    ]
    for host_alias, host_port in ports.items():
        lines.extend([
            "",
            f"Host {host_alias}",
            f"  SetEnv LC_STM_PROXY_PORT={host_port}",
        ])
    # Reset the matching context before parsing continues in the main config.
    lines.extend(["", "Host *", ""])
    _atomic_write(include_path, "\n".join(lines), "ssh-proxy-")
    return include_path


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
