from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import zipfile

from .models import HostConfig
from .resources import resource_path
from .store import default_data_dir


EXTENSION_ID = "elpco.ssh-tunnel-manager-env"
EXTENSION_VERSION = "1.0.1"
_INSTALL_LOCK = threading.Lock()


def proxy_map_path() -> Path:
    return default_data_dir() / "vscode-proxy-map.json"


def update_proxy_map(host: HostConfig) -> Path:
    path = proxy_map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"version": 1, "hosts": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            pass
    hosts = payload.get("hosts")
    if not isinstance(hosts, dict):
        hosts = {}
    hosts[host.alias] = {
        "port": int(host.remote_proxy_port),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    payload["version"] = 1
    payload["hosts"] = hosts

    fd, temporary_name = tempfile.mkstemp(prefix="vscode-proxy-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return path


def build_extension_vsix(destination: Path) -> Path:
    source = resource_path("vscode_extension")
    if not source.is_dir():
        raise FileNotFoundError(f"VSCode 配套组件资源不存在：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(source.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(source).as_posix())
    return destination


def _code_command(executable: Path, arguments: list[str]) -> list[str]:
    if executable.suffix.lower() in {".cmd", ".bat"}:
        return [
            os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
            str(executable), *arguments,
        ]
    return [str(executable), *arguments]


def _management_command(executable: Path, arguments: list[str]) -> list[str]:
    if executable.suffix.lower() == ".exe":
        cli = executable.parent / "bin" / "code.cmd"
        if cli.is_file():
            return _code_command(cli, arguments)
    return _code_command(executable, arguments)


def ensure_extension_installed(executable: Path, timeout: int = 45) -> tuple[bool, str]:
    with _INSTALL_LOCK:
        return _ensure_extension_installed(executable, timeout)


def _ensure_extension_installed(executable: Path, timeout: int) -> tuple[bool, str]:
    expected = f"{EXTENSION_ID}@{EXTENSION_VERSION}".casefold()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        listed = subprocess.run(
            _management_command(executable, ["--list-extensions", "--show-versions"]),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=flags,
        )
        installed = {line.strip().casefold() for line in listed.stdout.splitlines()}
        if listed.returncode == 0 and expected in installed:
            return True, f"{EXTENSION_ID} {EXTENSION_VERSION} 已安装"

        with tempfile.TemporaryDirectory(prefix="ssh-tunnel-vscode-") as folder:
            vsix = build_extension_vsix(Path(folder) / f"{EXTENSION_ID}-{EXTENSION_VERSION}.vsix")
            result = subprocess.run(
                _management_command(executable, ["--install-extension", str(vsix), "--force"]),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=flags,
            )
        detail = (result.stdout + "\n" + result.stderr).strip()
        ok = result.returncode == 0
        return ok, detail or ("安装成功" if ok else f"Code 退出码 {result.returncode}")
    except Exception as exc:
        return False, str(exc)
