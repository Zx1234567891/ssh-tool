from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import shlex
import socket
import subprocess

from .models import AppSettings, HostConfig, find_vscode_path
from .ssh_config import upsert_proxy_setenv


@dataclass
class ActionResult:
    ok: bool
    title: str
    detail: str


@dataclass
class RemoteDirectoryListing:
    path: str
    parent: str
    directories: list[tuple[str, str]]


class HostActions:
    def __init__(self, settings_provider) -> None:
        self._settings_provider = settings_provider

    @property
    def settings(self) -> AppSettings:
        return self._settings_provider()

    def _ssh(self, alias: str, remote_command: str | None = None) -> list[str]:
        settings = self.settings
        command = [
            settings.ssh_path, "-F", settings.ssh_config_path,
            "-o", "ClearAllForwardings=yes",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={settings.connect_timeout}",
            alias,
        ]
        if remote_command:
            command.append(remote_command)
        return command

    @staticmethod
    def _proxy_environment(port: int) -> dict[str, str]:
        proxy = f"http://127.0.0.1:{port}"
        return {
            "http_proxy": proxy,
            "https_proxy": proxy,
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }

    @classmethod
    def _remote_proxy_exports(cls, port: int) -> str:
        return " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in cls._proxy_environment(port).items()
        )

    def configure_local_ssh_proxy(self, host: HostConfig) -> Path:
        return upsert_proxy_setenv(
            self.settings.ssh_config_path, host.alias, host.remote_proxy_port
        )

    @staticmethod
    def _run(command: list[str], timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            command, input=input_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_local_proxy(self) -> ActionResult:
        settings = self.settings
        proxy = f"http://{settings.local_proxy_host}:{settings.local_proxy_port}"
        try:
            result = self._run(
                [
                    "curl.exe", "-sSIL", "--max-time", "12",
                    "-x", proxy, settings.proxy_test_url,
                ],
                16,
            )
        except FileNotFoundError:
            try:
                with socket.create_connection(
                    (settings.local_proxy_host, settings.local_proxy_port), timeout=3
                ):
                    return ActionResult(
                        True, "本地代理端口可连接",
                        f"{settings.local_proxy_host}:{settings.local_proxy_port}（系统无 curl，未验证 HTTPS）",
                    )
            except OSError as exc:
                return ActionResult(False, "本地代理不可用", str(exc))
        except Exception as exc:
            return ActionResult(False, "本地代理测试失败", str(exc))
        http_lines = [line.strip() for line in result.stdout.splitlines() if line.startswith("HTTP/")]
        connect_line = next(
            (line for line in http_lines if " 200 " in line and "Connection established" in line),
            "",
        )
        # A successful CONNECT proves the HTTP proxy accepted and forwarded the
        # tunnel.  Windows Schannel can still fail afterwards when the current
        # process has no usable TLS credentials; that is not a proxy failure.
        ok = bool(connect_line) or (result.returncode == 0 and bool(http_lines))
        detail = (connect_line or http_lines[0]) if ok else (
            result.stderr.strip() or result.stdout.strip() or f"curl 退出码 {result.returncode}"
        )
        return ActionResult(ok, "本地代理正常" if ok else "本地代理不可用", detail)

    def test_ssh(self, host: HostConfig) -> ActionResult:
        try:
            result = self._run(self._ssh(host.alias, "printf SSH_OK"), self.settings.connect_timeout + 5)
        except Exception as exc:
            return ActionResult(False, "SSH 测试失败", str(exc))
        ok = result.returncode == 0 and "SSH_OK" in result.stdout
        detail = result.stdout.strip() if ok else (result.stderr.strip() or result.stdout.strip())
        return ActionResult(ok, "SSH 连接正常" if ok else "SSH 测试失败", detail)

    def test_remote_proxy(self, host: HostConfig) -> ActionResult:
        port = host.remote_proxy_port
        url = shlex.quote(self.settings.proxy_test_url)
        remote = (
            f"curl -sSIL --max-time 20 -x http://127.0.0.1:{port} {url} "
            "| sed -n '1p'"
        )
        try:
            result = self._run(self._ssh(host.alias, remote), 28)
        except Exception as exc:
            return ActionResult(False, "远程代理测试失败", str(exc))
        first_line = result.stdout.strip().splitlines()[:1]
        detail = first_line[0] if first_line else result.stderr.strip()
        ok = result.returncode == 0 and detail.startswith("HTTP/")
        return ActionResult(ok, "远程代理正常" if ok else "远程代理测试失败", detail)

    def list_remote_directories(self, host: HostConfig, path: str) -> RemoteDirectoryListing:
        script = r'''import json
import os
import sys

requested = sys.argv[1] if len(sys.argv) > 1 else "~"
current = os.path.abspath(os.path.expanduser(requested))
if not os.path.isdir(current):
    raise NotADirectoryError(current)
directories = []
with os.scandir(current) as entries:
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=True):
                directories.append((entry.name, os.path.join(current, entry.name)))
        except OSError:
            continue
directories.sort(key=lambda item: item[0].casefold())
print(json.dumps({
    "path": current,
    "parent": os.path.dirname(current) or current,
    "directories": directories[:500],
}, ensure_ascii=False))
'''
        remote = "python3 - " + shlex.quote(path or "~")
        try:
            result = self._run(
                self._ssh(host.alias, remote), self.settings.connect_timeout + 15, script
            )
        except Exception as exc:
            raise RuntimeError(f"读取远程目录失败：{exc}") from exc
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "无法读取远程目录")
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            return RemoteDirectoryListing(
                path=payload["path"],
                parent=payload["parent"],
                directories=[(item[0], item[1]) for item in payload["directories"]],
            )
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"远程目录响应格式无效：{result.stdout.strip()}") from exc

    def configure_remote_shell(self, host: HostConfig) -> ActionResult:
        script = r'''set -eu
file="$HOME/.bashrc"
touch "$file"
stamp=$(date +%Y%m%d-%H%M%S)
cp "$file" "$file.ssh-tunnel-manager-backup-$stamp"
tmp=$(mktemp)
awk '
BEGIN {skip=0}
$0=="# >>> SSH Tunnel Manager >>>" {skip=1;next}
$0=="# <<< SSH Tunnel Manager <<<" {skip=0;next}
skip {next}
$0 ~ /^[[:space:]]*export[[:space:]]+(http_proxy|https_proxy|HTTP_PROXY|HTTPS_PROXY)="?http:\/\/(localhost|127[.]0[.]0[.]1):[0-9]+"?[[:space:]]*$/ {next}
{print}
' "$file" > "$tmp"
cat >> "$tmp" <<'EOF'
# >>> SSH Tunnel Manager >>>
stm_proxy_use() {
    case "$1" in
        ''|*[!0-9]*) echo "用法: stm_proxy_use <端口>" >&2; return 2 ;;
    esac
    export http_proxy="http://127.0.0.1:$1"
    export https_proxy="$http_proxy"
    export HTTP_PROXY="$http_proxy"
    export HTTPS_PROXY="$http_proxy"
    export NO_PROXY="localhost,127.0.0.1,::1"
    export no_proxy="$NO_PROXY"
    printf '代理已切换为 %s\n' "$http_proxy"
}

stm_proxy_off() {
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY no_proxy
    printf '代理已关闭\n'
}

if [ -n "${LC_STM_PROXY_PORT:-}" ]; then
    stm_proxy_use "$LC_STM_PROXY_PORT" >/dev/null
fi
# <<< SSH Tunnel Manager <<<
EOF
mv "$tmp" "$file"
printf 'CONFIG_OK backup=%s\n' "$file.ssh-tunnel-manager-backup-$stamp"
'''
        try:
            command = self._ssh(host.alias)
            # Windows pipes can turn LF into CRLF (or CRCRLF through some
            # OpenSSH builds). Strip CR before Bash parses variable values.
            command.append("tr -d '\\015' | bash -s")
            result = self._run(command, 20, script)
        except Exception as exc:
            return ActionResult(False, "配置失败", str(exc))
        ok = result.returncode == 0 and "CONFIG_OK" in result.stdout
        detail = result.stdout.strip() if ok else (result.stderr.strip() or result.stdout.strip())
        return ActionResult(ok, "远程代理切换器已安装" if ok else "配置失败", detail)

    def smoke_codex(self, host: HostConfig) -> ActionResult:
        timeout = self.settings.smoke_timeout
        port = host.remote_proxy_port
        inner = (
            f"export http_proxy=http://127.0.0.1:{port} https_proxy=http://127.0.0.1:{port} "
            f"HTTP_PROXY=http://127.0.0.1:{port} HTTPS_PROXY=http://127.0.0.1:{port}; "
            f"timeout {timeout}s codex exec --skip-git-repo-check -"
        )
        # Conda/npm-installed Codex is often added to PATH by interactive shell setup.
        remote = "bash -lic " + shlex.quote(inner)
        try:
            result = self._run(
                self._ssh(host.alias, remote), timeout + 12,
                "Reply exactly: CODEX_SMOKE_OK\n",
            )
        except Exception as exc:
            return ActionResult(False, "Codex 冒烟测试失败", str(exc))
        combined = (result.stdout + "\n" + result.stderr).strip()
        ok = result.returncode == 0 and "CODEX_SMOKE_OK" in combined
        tail = "\n".join(combined.splitlines()[-8:])
        return ActionResult(ok, "Codex 测试通过" if ok else "Codex 冒烟测试失败", tail)

    def launch_terminal(self, host: HostConfig) -> None:
        exports = self._remote_proxy_exports(host.remote_proxy_port)
        remote = f"export {exports}; exec bash -l"
        subprocess.Popen(
            [self.settings.ssh_path, "-F", self.settings.ssh_config_path,
             "-o", "ClearAllForwardings=yes", "-t", host.alias, remote],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )

    def launch_vscode(self, host: HostConfig) -> None:
        executable = self.settings.vscode_path or find_vscode_path()
        if not executable:
            raise FileNotFoundError("未找到 VSCode。请在“设置 → VSCode 程序”中选择 Code.exe。")
        path = Path(executable)
        if not path.is_file():
            executable = find_vscode_path()
            path = Path(executable) if executable else path
        if not path.is_file():
            raise FileNotFoundError(f"VSCode 路径不存在：{path}。请在设置中重新选择。")
        self.settings.vscode_path = str(path)
        self.configure_local_ssh_proxy(host)
        arguments = [
            "--new-window",
            "--remote", f"ssh-remote+{host.alias}", host.remote_dir,
        ]
        if path.suffix.lower() in {".cmd", ".bat"}:
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path), *arguments]
        else:
            command = [str(path), *arguments]
        subprocess.Popen(
            command,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def launch_codex(self, host: HostConfig) -> None:
        exports = self._remote_proxy_exports(host.remote_proxy_port)
        remote = "bash -lic " + shlex.quote(f"export {exports}; exec codex")
        subprocess.Popen(
            [self.settings.ssh_path, "-F", self.settings.ssh_config_path,
             "-o", "ClearAllForwardings=yes", "-t", host.alias, remote],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
