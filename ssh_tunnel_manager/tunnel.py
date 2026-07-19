from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import subprocess
import threading
import time
from typing import Callable

from .models import AppSettings, HostConfig


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
            if runtime.process and runtime.process.poll() is None:
                return
            runtime.generation += 1
            generation = runtime.generation
            runtime.stop_requested = False
            runtime.state = TunnelState.CONNECTING
            runtime.message = "正在连接"
        self._emit(host.alias, TunnelState.CONNECTING, "正在建立专用隧道…")
        threading.Thread(target=self._supervise, args=(host, generation), daemon=True).start()

    def stop(self, alias: str) -> None:
        runtime = self.runtime(alias)
        with self._lock:
            runtime.generation += 1
            runtime.stop_requested = True
            process = runtime.process
        if process and process.poll() is None:
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

    def stop_all(self) -> None:
        for alias in list(self._items):
            self.stop(alias)

    def _emit(self, alias: str, state: TunnelState, message: str) -> None:
        self._event_callback(alias, state, message)

    def _command(self, host: HostConfig) -> list[str]:
        settings = self._settings_provider()
        forwarding = f"{host.remote_proxy_port}:{settings.local_proxy_host}:{settings.local_proxy_port}"
        return [
            settings.ssh_path, "-F", settings.ssh_config_path, "-NT",
            "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", f"ConnectTimeout={settings.connect_timeout}",
            "-o", f"ServerAliveInterval={settings.keepalive_interval}",
            "-o", f"ServerAliveCountMax={settings.keepalive_count_max}",
            "-R", forwarding, host.alias,
        ]

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
                try:
                    return_code = process.wait(timeout=1.2)
                except subprocess.TimeoutExpired:
                    return_code = None
                if return_code is None:
                    attempt = 0
                    with self._lock:
                        runtime.state = TunnelState.CONNECTED
                        runtime.message = "运行中"
                    self._emit(host.alias, TunnelState.CONNECTED, "隧道已连接")
                    return_code = process.wait()
                error = process.stderr.read().strip() if process.stderr else ""
                if runtime.stop_requested or runtime.generation != generation:
                    return
                message = error.splitlines()[-1] if error else f"SSH 已退出（{return_code}）"
            except Exception as exc:
                message = str(exc)

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
            self._emit(host.alias, TunnelState.RETRYING, f"连接中断，{delay} 秒后重试：{message}")
            for _ in range(delay * 10):
                if runtime.stop_requested or runtime.generation != generation:
                    return
                time.sleep(0.1)
