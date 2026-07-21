from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import urllib.request

from .logging_system import log_event


logger = logging.getLogger("ssh_tunnel_manager.updater")


@dataclass(slots=True)
class UpdateInfo:
    version: str
    name: str
    notes: str
    page_url: str
    asset_url: str
    asset_name: str
    sha256: str


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.split("-", 1)[0])
    return tuple(int(item) for item in numbers[:4]) or (0,)


def check_for_update(repository: str, current_version: str, timeout: int = 10) -> UpdateInfo | None:
    repository = repository.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("更新仓库必须使用 owner/repository 格式")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "SshTunnelManager"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    version = str(payload.get("tag_name") or "").lstrip("vV")
    if _version_tuple(version) <= _version_tuple(current_version):
        return None
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    installer = next(
        (
            item for item in assets
            if isinstance(item, dict)
            and str(item.get("name", "")).lower().endswith(".exe")
            and "setup" in str(item.get("name", "")).lower()
        ),
        None,
    )
    if not installer:
        raise RuntimeError("最新 Release 没有 Windows Setup 安装器")
    digest = str(installer.get("digest") or "")
    sha256 = digest.split(":", 1)[1] if digest.lower().startswith("sha256:") else ""
    if not sha256:
        checksum_asset = next(
            (
                item for item in assets
                if isinstance(item, dict)
                and str(item.get("name", "")).casefold() == "sha256sums.txt"
            ),
            None,
        )
        if checksum_asset and checksum_asset.get("browser_download_url"):
            checksum_request = urllib.request.Request(
                str(checksum_asset["browser_download_url"]),
                headers={"User-Agent": "SshTunnelManager"},
            )
            with urllib.request.urlopen(checksum_request, timeout=timeout) as response:
                checksum_text = response.read().decode("ascii", errors="replace")
            for line in checksum_text.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and Path(parts[1].strip().lstrip("*")).name == str(installer.get("name")):
                    sha256 = parts[0]
                    break
    info = UpdateInfo(
        version=version,
        name=str(payload.get("name") or payload.get("tag_name") or version),
        notes=str(payload.get("body") or ""),
        page_url=str(payload.get("html_url") or ""),
        asset_url=str(installer.get("browser_download_url") or ""),
        asset_name=str(installer.get("name") or f"SshTunnelManager-Setup-{version}.exe"),
        sha256=sha256,
    )
    log_event(logger, logging.INFO, "update.available", version=version, asset=info.asset_name)
    return info


def download_update(info: UpdateInfo, update_dir: Path, timeout: int = 120) -> Path:
    if not info.asset_url:
        raise RuntimeError("Release 安装器没有下载地址")
    if not info.sha256:
        raise RuntimeError("Release 安装器没有 SHA-256 摘要，已拒绝自动安装")
    update_dir.mkdir(parents=True, exist_ok=True)
    target = update_dir / info.asset_name
    temporary = target.with_suffix(target.suffix + ".download")
    request = urllib.request.Request(info.asset_url, headers={"User-Agent": "SshTunnelManager"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=timeout) as response, temporary.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual.casefold() != info.sha256.casefold():
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"安装器 SHA-256 校验失败：期望 {info.sha256}，实际 {actual}")
    temporary.replace(target)
    log_event(logger, logging.INFO, "update.downloaded", version=info.version, path=str(target))
    return target


def launch_installer(path: Path) -> None:
    subprocess.Popen([str(path), "/NORESTART"], close_fds=True)
    log_event(logger, logging.INFO, "update.installer_started", path=str(path))
