from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import logging
import time

from .actions import ActionResult, HostActions
from .logging_system import log_event
from .models import HostConfig


logger = logging.getLogger("ssh_tunnel_manager.health")


class HealthState(str, Enum):
    UNKNOWN = "unknown"
    TESTING = "testing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


HEALTH_NODE_LABELS = {
    "codex": "远程 Codex",
    "remote_port": "远程端口",
    "ssh_tunnel": "SSH 隧道",
    "local_proxy": "本机 Clash",
    "clash_node": "Clash 节点",
    "openai": "OpenAI",
}


@dataclass(slots=True)
class HealthNodeResult:
    node: str
    state: HealthState
    title: str
    detail: str = ""
    duration_ms: int = 0
    checked_at: str = ""
    last_success_at: str = ""
    last_failure_at: str = ""
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "state": self.state.value,
            "title": self.title,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "checked_at": self.checked_at,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass(slots=True)
class HealthSnapshot:
    host_id: str
    host_alias: str
    started_at: str
    completed_at: str = ""
    nodes: dict[str, HealthNodeResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "host_id": self.host_id,
            "host_alias": self.host_alias,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "nodes": {key: value.to_dict() for key, value in self.nodes.items()},
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class HealthProbeService:
    def __init__(self, actions: HostActions) -> None:
        self.actions = actions
        self._history: dict[tuple[str, str], dict] = {}

    def _record(self, host: HostConfig, result: HealthNodeResult) -> HealthNodeResult:
        key = (host.id, result.node)
        history = self._history.setdefault(key, {
            "last_success_at": "", "last_failure_at": "", "consecutive_failures": 0,
        })
        if result.state == HealthState.HEALTHY:
            history["last_success_at"] = result.checked_at
            history["consecutive_failures"] = 0
        elif result.state == HealthState.FAILED:
            history["last_failure_at"] = result.checked_at
            history["consecutive_failures"] = int(history["consecutive_failures"]) + 1
        result.last_success_at = str(history["last_success_at"])
        result.last_failure_at = str(history["last_failure_at"])
        result.consecutive_failures = int(history["consecutive_failures"])
        return result

    @staticmethod
    def _from_action(node: str, result: ActionResult, started: float) -> HealthNodeResult:
        duration = int((time.monotonic() - started) * 1000)
        state = HealthState.HEALTHY if result.ok else HealthState.FAILED
        return HealthNodeResult(
            node=node, state=state, title=result.title, detail=result.detail,
            duration_ms=duration, checked_at=_now(),
        )

    def _probe(self, host: HostConfig, node: str, function) -> HealthNodeResult:
        started = time.monotonic()
        try:
            result = function()
        except Exception as exc:
            result = ActionResult(False, f"{HEALTH_NODE_LABELS[node]}检测失败", str(exc))
        value = self._record(host, self._from_action(node, result, started))
        log_event(
            logger, logging.INFO if result.ok else logging.WARNING, "health.probe",
            host=host.alias, host_id=host.id, node=node, state=value.state.value,
            duration_ms=value.duration_ms, detail=value.detail,
        )
        return value

    def run_full(self, host: HostConfig, tunnel_state: str) -> HealthSnapshot:
        snapshot = HealthSnapshot(host.id, host.alias, _now())

        snapshot.nodes["local_proxy"] = self._probe(
            host, "local_proxy", self.actions.test_local_proxy
        )

        if tunnel_state == "connected":
            snapshot.nodes["ssh_tunnel"] = self._record(host, HealthNodeResult(
                "ssh_tunnel", HealthState.HEALTHY, "SSH 隧道运行中",
                "专用 SSH 转发进程仍在运行", checked_at=_now(),
            ))
        elif tunnel_state in {"connecting", "retrying"}:
            snapshot.nodes["ssh_tunnel"] = self._record(host, HealthNodeResult(
                "ssh_tunnel", HealthState.DEGRADED, "SSH 隧道正在恢复",
                tunnel_state, checked_at=_now(),
            ))
        elif tunnel_state == "error":
            snapshot.nodes["ssh_tunnel"] = self._record(host, HealthNodeResult(
                "ssh_tunnel", HealthState.FAILED, "SSH 隧道异常",
                "查看隧道日志或重新启动", checked_at=_now(),
            ))
        else:
            snapshot.nodes["ssh_tunnel"] = self._record(host, HealthNodeResult(
                "ssh_tunnel", HealthState.UNKNOWN, "SSH 隧道未启动",
                "启动隧道后才能检测远程代理", checked_at=_now(),
            ))

        snapshot.nodes["codex"] = self._probe(
            host, "codex", lambda: self.actions.test_codex_available(host)
        )

        if tunnel_state == "connected":
            snapshot.nodes["remote_port"] = self._probe(
                host, "remote_port", lambda: self.actions.test_remote_port(host)
            )
        else:
            snapshot.nodes["remote_port"] = self._record(host, HealthNodeResult(
                "remote_port", HealthState.UNKNOWN, "远程端口未检测",
                "SSH 隧道未连接", checked_at=_now(),
            ))

        if self.actions.settings.clash_controller_url.strip():
            snapshot.nodes["clash_node"] = self._probe(
                host, "clash_node", self.actions.test_clash_controller
            )
        else:
            snapshot.nodes["clash_node"] = self._record(host, HealthNodeResult(
                "clash_node", HealthState.UNKNOWN, "未配置 Clash Controller",
                "本机代理仍可独立检测", checked_at=_now(),
            ))

        local_ok = snapshot.nodes["local_proxy"].state == HealthState.HEALTHY
        remote_ok = snapshot.nodes["remote_port"].state == HealthState.HEALTHY
        if local_ok and remote_ok:
            snapshot.nodes["openai"] = self._probe(
                host, "openai", lambda: self.actions.test_openai_chain(host)
            )
        else:
            reason = "本机代理异常" if not local_ok else "远程代理端口异常"
            snapshot.nodes["openai"] = self._record(host, HealthNodeResult(
                "openai", HealthState.UNKNOWN, "OpenAI 未检测", reason, checked_at=_now(),
            ))

        snapshot.completed_at = _now()
        log_event(
            logger, logging.INFO, "health.completed", host=host.alias, host_id=host.id,
            states=",".join(f"{key}:{value.state.value}" for key, value in snapshot.nodes.items()),
        )
        return snapshot
