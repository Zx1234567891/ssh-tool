from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from collections import deque
import ctypes
import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Callable

from .models import AppSettings, HostConfig
from .logging_system import log_event, redact


logger = logging.getLogger("ssh_tunnel_manager.tunnel")


class TunnelState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RETRYING = "retrying"
    ERROR = "error"


@dataclass
class TunnelRuntime:
    state: TunnelState = TunnelState.STOPPED
    process: subprocess.Popen | None = None
    message: str = "未启动"
    started_at: float | None = None
    stop_requested: bool = False
    generation: int = 0
    external_pid: int | None = None
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=80))
    reconnect_count: int = 0


class TunnelManager:
    def __init__(self, settings_provider: Callable[[], AppSettings], event_callback: Callable[[str, TunnelState, str], None]) -> None:
        self._settings_provider = settings_provider
        self._event_callback = event_callback
        self._items: dict[str, TunnelRuntime] = {}
        self._lock = threading.RLock()

    def runtime(self, alias: str) -> TunnelRuntime:
        with self._lock:
            return self._items.setdefault(alias, TunnelRuntime())

    def start(self, host: HostConfig) -> None:
        runtime = self.runtime(host.alias)
        with self._lock:
            if runtime.external_pid and self._pid_is_running(runtime.external_pid):
                runtime.state = TunnelState.CONNECTED
                runtime.message = "由另一助手实例管理"
                self._emit(host.alias, TunnelState.CONNECTED, runtime.message)
                return
            runtime.external_pid = None
            if runtime.process and runtime.process.poll() is None:
                return
            runtime.generation += 1
            generation = runtime.generation
            runtime.stop_requested = False
            runtime.state = TunnelState.CONNECTING
            runtime.message = "正在连接"
            runtime.stderr_tail.clear()
        log_event(
            logger, logging.INFO, "tunnel.start_requested",
            host=host.alias, host_id=host.id, generation=generation,
            remote_port=host.remote_proxy_port,
        )
        self._emit(host.alias, TunnelState.CONNECTING, "正在建立专用隧道…")
        threading.Thread(target=self._supervise, args=(host, generation), daemon=True).start()

    def stop(self, alias: str) -> None:
        runtime = self.runtime(alias)
        with self._lock:
            if runtime.external_pid and self._pid_is_running(runtime.external_pid):
                runtime.state = TunnelState.CONNECTED
                runtime.message = "由另一助手实例管理，请在原窗口停止"
                self._emit(alias, TunnelState.CONNECTED, runtime.message)
                return
            runtime.external_pid = None
            runtime.generation += 1
            runtime.stop_requested = True
            process = runtime.process
        if process and process.poll() is None:
            log_event(logger, logging.INFO, "tunnel.stop_requested", host=alias, pid=process.pid)
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        with self._lock:
            runtime.process = None
            runtime.state = TunnelState.STOPPED
            runtime.message = "已停止"
        self._emit(alias, TunnelState.STOPPED, "隧道已停止")
        log_event(logger, logging.INFO, "tunnel.stopped", host=alias)

    def stop_all(self) -> None:
        for alias in list(self._items):
            self.stop(alias)

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        if os.name != "nt":
            return False
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    @staticmethod
    def _existing_ssh_processes() -> list[dict]:
        if os.name != "nt":
            return []
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name = 'ssh.exe'\" | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []
            payload = json.loads(result.stdout)
            return payload if isinstance(payload, list) else [payload]
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return []

    def discover_existing(self, hosts: list[HostConfig]) -> list[str]:
        """Mark matching tunnels owned by another running app instance as connected."""
        settings = self._settings_provider()
        discovered: list[str] = []
        for row in self._existing_ssh_processes():
            command = str(row.get("CommandLine") or "")
            pid = int(row.get("ProcessId") or 0)
            if not pid or " -NT " not in f" {command} ":
                continue
            for host in hosts:
                forwarding = (
                    f"{host.remote_proxy_port}:{settings.local_proxy_host}:"
                    f"{settings.local_proxy_port}"
                )
                has_forward = re.search(
                    rf"(?:^|\s)-R\s+\"?{re.escape(forwarding)}\"?(?:\s|$)", command
                )
                has_alias = re.search(
                    rf"(?:^|\s)\"?{re.escape(host.alias)}\"?\s*$", command,
                    flags=re.IGNORECASE,
                )
                if not (has_forward and has_alias):
                    continue
                runtime = self.runtime(host.alias)
                if runtime.process and runtime.process.poll() is None:
                    continue
                runtime.external_pid = pid
                runtime.state = TunnelState.CONNECTED
                runtime.message = "由另一助手实例管理"
                log_event(
                    logger, logging.INFO, "tunnel.external_discovered",
                    host=host.alias, host_id=host.id, pid=pid, forwarding=forwarding,
                )
                if host.alias not in discovered:
                    discovered.append(host.alias)
        return discovered

    def check_external(self) -> None:
        with self._lock:
            external = [
                (alias, runtime) for alias, runtime in self._items.items()
                if runtime.external_pid
            ]
        for alias, runtime in external:
            if runtime.external_pid and self._pid_is_running(runtime.external_pid):
                continue
            with self._lock:
                runtime.external_pid = None
                runtime.state = TunnelState.STOPPED
                runtime.message = "外部隧道已停止"
            self._emit(alias, TunnelState.STOPPED, "外部隧道已停止，可由本窗口重新启动")

    def _emit(self, alias: str, state: TunnelState, message: str) -> None:
        self._event_callback(alias, state, message)

    def _command(self, host: HostConfig) -> list[str]:
        settings = self._settings_provider()
        forwarding = f"{host.remote_proxy_port}:{settings.local_proxy_host}:{settings.local_proxy_port}"
        command = [
            settings.ssh_path, "-F", settings.ssh_config_path, "-NT",
            "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", f"ConnectTimeout={settings.connect_timeout}",
            "-o", f"ServerAliveInterval={settings.keepalive_interval}",
            "-o", f"ServerAliveCountMax={settings.keepalive_count_max}",
            "-R", forwarding, host.alias,
        ]
        if settings.ssh_debug_logging:
            command.insert(4, "-vv")
        return command

    def _read_stderr(self, host: HostConfig, runtime: TunnelRuntime, process: subprocess.Popen) -> None:
        if not process.stderr:
            return
        try:
            for raw in iter(process.stderr.readline, ""):
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                with self._lock:
                    runtime.stderr_tail.append(line)
                log_event(
                    logger, logging.DEBUG, "tunnel.ssh_stderr",
                    host=host.alias, pid=process.pid, line=redact(line),
                )
        except (OSError, ValueError) as exc:
            log_event(
                logger, logging.DEBUG, "tunnel.stderr_reader_stopped",
                host=host.alias, pid=process.pid, error=str(exc),
            )

    def _supervise(self, host: HostConfig, generation: int) -> None:
        attempt = 0
        delays = [2, 4, 8, 15, 30]
        while True:
            runtime = self.runtime(host.alias)
            if runtime.stop_requested or runtime.generation != generation:
                return
            try:
                process = subprocess.Popen(
                    self._command(host), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                with self._lock:
                    runtime.process = process
                    runtime.started_at = time.time()
                log_event(
                    logger, logging.INFO, "tunnel.process_started",
                    host=host.alias, host_id=host.id, pid=process.pid, generation=generation,
                    command=" ".join(self._command(host)),
                )
                stderr_thread = threading.Thread(
                    target=self._read_stderr, args=(host, runtime, process), daemon=True
                )
                stderr_thread.start()
                try:
                    return_code = process.wait(timeout=1.2)
                except subprocess.TimeoutExpired:
                    return_code = None
                if return_code is None:
                    attempt = 0
                    with self._lock:
                        runtime.state = TunnelState.CONNECTED
                        runtime.message = "运行中"
                        runtime.reconnect_count = attempt
                    self._emit(host.alias, TunnelState.CONNECTED, "隧道已连接")
                    log_event(
                        logger, logging.INFO, "tunnel.connected",
                        host=host.alias, pid=process.pid, generation=generation,
                    )
                    return_code = process.wait()
                stderr_thread.join(timeout=1)
                with self._lock:
                    error = "\n".join(runtime.stderr_tail)
                if runtime.stop_requested or runtime.generation != generation:
                    return
                message = error.splitlines()[-1] if error else f"SSH 已退出（{return_code}）"
                log_event(
                    logger, logging.WARNING, "tunnel.process_exited",
                    host=host.alias, pid=process.pid, generation=generation,
                    exit_code=return_code, error=message,
                )
            except Exception as exc:
                message = str(exc)
                log_event(
                    logger, logging.ERROR, "tunnel.start_failed",
                    host=host.alias, generation=generation, error=message,
                )

            with self._lock:
                runtime.process = None
            if not host.auto_reconnect or runtime.stop_requested or runtime.generation != generation:
                with self._lock:
                    runtime.state = TunnelState.ERROR
                    runtime.message = message
                self._emit(host.alias, TunnelState.ERROR, message)
                return
            delay = delays[min(attempt, len(delays) - 1)]
            attempt += 1
            with self._lock:
                runtime.state = TunnelState.RETRYING
                runtime.message = f"{delay} 秒后重连"
                runtime.reconnect_count += 1
            self._emit(host.alias, TunnelState.RETRYING, f"连接中断，{delay} 秒后重试：{message}")
            log_event(
                logger, logging.WARNING, "tunnel.retry_scheduled",
                host=host.alias, generation=generation, delay_seconds=delay,
                reconnect_count=runtime.reconnect_count, error=message,
            )
            for _ in range(delay * 10):
                if runtime.stop_requested or runtime.generation != generation:
                    return
                time.sleep(0.1)
