from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import logging
import os
import shlex
import socket
import subprocess
import urllib.error
import urllib.request

from .models import AppSettings, HostConfig, find_vscode_path
from .ssh_config import upsert_proxy_setenv
from .logging_system import log_event


logger = logging.getLogger("ssh_tunnel_manager.actions")


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
        result_value = ActionResult(ok, "本地代理正常" if ok else "本地代理不可用", detail)
        log_event(
            logger, logging.INFO if ok else logging.WARNING, "probe.local_proxy",
            ok=ok, proxy=proxy, detail=detail,
        )
        return result_value

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

    def test_remote_port(self, host: HostConfig) -> ActionResult:
        port = int(host.remote_proxy_port)
        script = (
            "import socket,sys; "
            f"s=socket.create_connection(('127.0.0.1',{port}),5); "
            "s.close(); print('REMOTE_PORT_OK')"
        )
        remote = "python3 -c " + shlex.quote(script)
        try:
            result = self._run(self._ssh(host.alias, remote), self.settings.connect_timeout + 8)
        except Exception as exc:
            return ActionResult(False, "远程端口不可用", str(exc))
        ok = result.returncode == 0 and "REMOTE_PORT_OK" in result.stdout
        detail = result.stdout.strip() if ok else (result.stderr.strip() or result.stdout.strip())
        return ActionResult(ok, "远程端口正常" if ok else "远程端口不可用", detail)

    def test_openai_chain(self, host: HostConfig) -> ActionResult:
        port = int(host.remote_proxy_port)
        url = shlex.quote(self.settings.openai_test_url)
        remote = (
            f"curl -sS -o /dev/null --max-time 25 -x http://127.0.0.1:{port} "
            "-w 'OPENAI_HTTP=%{http_code} CONNECT=%{time_connect} TLS=%{time_appconnect} TOTAL=%{time_total}' "
            f"{url}"
        )
        try:
            result = self._run(self._ssh(host.alias, remote), 32)
        except Exception as exc:
            return ActionResult(False, "OpenAI 链路检测失败", str(exc))
        metrics = result.stdout.strip()
        error = result.stderr.strip()
        detail = metrics or error
        if result.returncode != 0:
            detail = f"{detail} CURL_EXIT={result.returncode}".strip()
            if error and error not in detail:
                detail += f" ERROR={error}"
        ok = result.returncode == 0 and "OPENAI_HTTP=" in detail and "OPENAI_HTTP=000" not in detail
        return ActionResult(ok, "OpenAI 链路正常" if ok else "OpenAI 链路检测失败", detail)

    def test_codex_available(self, host: HostConfig) -> ActionResult:
        remote = "bash -lic " + shlex.quote(
            "command -v codex >/dev/null 2>&1 && codex --version || exit 127"
        )
        try:
            result = self._run(self._ssh(host.alias, remote), self.settings.connect_timeout + 10)
        except Exception as exc:
            return ActionResult(False, "Codex 检测失败", str(exc))
        ok = result.returncode == 0 and bool(result.stdout.strip())
        detail = result.stdout.strip() if ok else (result.stderr.strip() or "远程未找到 codex")
        return ActionResult(ok, "Codex 可用" if ok else "Codex 不可用", detail)

    def test_clash_controller(self) -> ActionResult:
        base = self.settings.clash_controller_url.strip().rstrip("/")
        if not base:
            return ActionResult(False, "未配置 Clash Controller", "在设置中填写 Controller 地址后可显示节点")
        request = urllib.request.Request(base + "/proxies")
        if self.settings.clash_controller_secret:
            request.add_header("Authorization", f"Bearer {self.settings.clash_controller_secret}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
            proxies = payload.get("proxies", {}) if isinstance(payload, dict) else {}
            selected: list[str] = []
            for name, value in proxies.items():
                if isinstance(value, dict) and value.get("now"):
                    selected.append(f"{name} → {value['now']}")
            detail = "；".join(selected[:4]) or f"Controller 正常，共 {len(proxies)} 个代理项"
            return ActionResult(True, "Clash Controller 正常", detail)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return ActionResult(False, "Clash Controller 不可用", str(exc))

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
        log_event(logger, logging.INFO, "launch.terminal", host=host.alias, host_id=host.id)

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
        log_event(
            logger, logging.INFO, "launch.vscode",
            host=host.alias, host_id=host.id, remote_dir=host.remote_dir,
        )

    def launch_codex(self, host: HostConfig) -> str:
        exports = self._remote_proxy_exports(host.remote_proxy_port)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_dir = f"$HOME/.codex/log/ssh-tunnel-manager/{host.alias}/{stamp}"
        inner = (
            f'mkdir -p "{log_dir}"; export {exports}; '
            f"export RUST_LOG={shlex.quote(self.settings.codex_log_level)}; "
            f'exec codex -c log_dir="{log_dir}"'
        )
        remote = "bash -lic " + shlex.quote(inner)
        subprocess.Popen(
            [self.settings.ssh_path, "-F", self.settings.ssh_config_path,
             "-o", "ClearAllForwardings=yes", "-t", host.alias, remote],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        log_event(
            logger, logging.INFO, "launch.codex",
            host=host.alias, host_id=host.id, remote_log_dir=log_dir,
            log_level=self.settings.codex_log_level,
        )
        return log_dir

    def latest_codex_log(self, host: HostConfig, lines: int = 300) -> ActionResult:
        root = f"$HOME/.codex/log/ssh-tunnel-manager/{host.alias}"
        remote = (
            f'file=$(find "{root}" -type f -name codex-tui.log 2>/dev/null | sort | tail -n 1); '
            f'[ -n "$file" ] || {{ echo "未找到 Codex 日志" >&2; exit 2; }}; '
            f'echo "FILE=$file"; tail -n {max(20, min(lines, 2000))} "$file"'
        )
        try:
            result = self._run(self._ssh(host.alias, remote), self.settings.connect_timeout + 15)
        except Exception as exc:
            return ActionResult(False, "读取 Codex 日志失败", str(exc))
        ok = result.returncode == 0
        detail = result.stdout.strip() if ok else (result.stderr.strip() or result.stdout.strip())
        return ActionResult(ok, "Codex 日志" if ok else "读取 Codex 日志失败", detail)

    def vscode_log_directory(self) -> Path | None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        candidates = [Path(appdata) / "Code" / "logs", Path(appdata) / "Code - Insiders" / "logs"]
        existing = [path for path in candidates if path.is_dir()]
        return max(existing, key=lambda path: path.stat().st_mtime) if existing else None

    def open_vscode_logs(self) -> Path:
        path = self.vscode_log_directory()
        if not path:
            raise FileNotFoundError("未找到 VSCode 日志目录")
        os.startfile(path)  # type: ignore[attr-defined]
        log_event(logger, logging.INFO, "vscode.logs_opened", path=str(path))
        return path

    def latest_remote_vscode_log(self, host: HostConfig, lines: int = 300) -> ActionResult:
        limit = max(20, min(lines, 2000))
        remote = (
            "file=$(find \"$HOME\"/.vscode-server* -type f "
            "\\( -name 'log.txt' -o -name '*.log' \\) -printf '%T@ %p\\n' 2>/dev/null "
            "| sort -n | tail -n 1 | cut -d' ' -f2-); "
            "[ -n \"$file\" ] || { echo '未找到 VSCode Server 日志' >&2; exit 2; }; "
            f"echo \"FILE=$file\"; tail -n {limit} \"$file\""
        )
        try:
            result = self._run(self._ssh(host.alias, remote), self.settings.connect_timeout + 15)
        except Exception as exc:
            return ActionResult(False, "读取 VSCode Server 日志失败", str(exc))
        ok = result.returncode == 0
        detail = result.stdout.strip() if ok else (result.stderr.strip() or result.stdout.strip())
        return ActionResult(ok, "VSCode Server 日志" if ok else "读取 VSCode Server 日志失败", detail)
