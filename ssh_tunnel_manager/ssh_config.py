from __future__ import annotations

from pathlib import Path
import glob
import shlex
import subprocess

from .models import ResolvedHost


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
