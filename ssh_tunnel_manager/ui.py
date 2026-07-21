from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from pathlib import Path
import logging
import os
import traceback

from PyQt6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QStyle,
    QSystemTrayIcon, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .actions import ActionResult, HostActions
from . import __version__
from .dialogs import (
    HostDialog, RemoteFolderDialog, SettingsDialog, StructuredLogDialog, TextViewerDialog,
)
from .diagnostics import create_diagnostic_bundle
from .health import (
    HEALTH_NODE_LABELS, HealthNodeResult, HealthProbeService, HealthSnapshot, HealthState,
)
from .logging_system import configure_logging, log_event, shutdown_logging
from .models import AppState, HostConfig, connected_hosts_first
from .resources import resource_path
from .ssh_config import append_host_entry, parse_host_aliases, resolve_host
from .store import StateStore, UnsupportedConfigVersion
from .theme import STATE_BACKGROUNDS, STATE_COLORS, STATE_TEXT
from .tunnel import TunnelManager, TunnelState
from .updater import UpdateInfo, check_for_update, download_update, launch_installer


logger = logging.getLogger("ssh_tunnel_manager.ui")


ITEM_KIND_ROLE = int(Qt.ItemDataRole.UserRole)
HOST_ID_ROLE = ITEM_KIND_ROLE + 1
WORKSPACE_PATH_ROLE = ITEM_KIND_ROLE + 2


HEALTH_COLORS = {
    HealthState.UNKNOWN: ("#89909a", "#eef0f2"),
    HealthState.TESTING: ("#2f80ed", "#eaf3ff"),
    HealthState.HEALTHY: ("#07c160", "#e8f8ef"),
    HealthState.DEGRADED: ("#e6a23c", "#fff5e6"),
    HealthState.FAILED: ("#e05252", "#fdeeee"),
}


class HealthNodeWidget(QFrame):
    def __init__(self, node: str) -> None:
        super().__init__()
        self.node = node
        self.setMinimumWidth(78)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 8, 9, 8)
        layout.setSpacing(3)
        self.name = QLabel(HEALTH_NODE_LABELS[node])
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail = QLabel("未检测")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        self.detail.setToolTip("尚未执行链路检测")
        layout.addWidget(self.name)
        layout.addWidget(self.detail)
        self.set_state(HealthState.UNKNOWN, "未检测", "尚未执行链路检测")

    def set_state(self, state: HealthState, text: str, tooltip: str = "") -> None:
        color, background = HEALTH_COLORS[state]
        self.setStyleSheet(
            f"QFrame {{background:{background}; border:1px solid {color}; border-radius:8px;}}"
            "QLabel {border:none; background:transparent;}"
        )
        self.detail.setText(text)
        self.detail.setStyleSheet(f"color:{color}; font-size:11px; font-weight:600;")
        self.setToolTip(tooltip)
        self.detail.setToolTip(tooltip)

    def set_result(self, result: HealthNodeResult) -> None:
        text = {
            HealthState.HEALTHY: "正常",
            HealthState.DEGRADED: "不稳定",
            HealthState.FAILED: "失败",
            HealthState.TESTING: "检测中",
            HealthState.UNKNOWN: "未知",
        }[result.state]
        if result.state == HealthState.UNKNOWN and result.title.startswith("未配置"):
            text = "未配置"
        if result.duration_ms:
            text += f" · {result.duration_ms}ms"
        history = (
            f"\n最后成功：{result.last_success_at or '无'}"
            f"\n最后失败：{result.last_failure_at or '无'}"
            f"\n连续失败：{result.consecutive_failures} 次"
        )
        tooltip = f"{result.title}\n{result.detail}\n最后检测：{result.checked_at}{history}".strip()
        self.set_state(result.state, text, tooltip)


class HealthChainWidget(QFrame):
    NODE_ORDER = ["codex", "remote_port", "ssh_tunnel", "local_proxy", "clash_node", "openai"]

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        title_row = QHBoxLayout()
        title = QLabel("链路健康")
        title.setObjectName("sectionTitle")
        self.summary = QLabel("选择主机后检测")
        self.summary.setObjectName("muted")
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(self.summary)
        layout.addLayout(title_row)
        chain = QHBoxLayout()
        chain.setSpacing(6)
        self.nodes: dict[str, HealthNodeWidget] = {}
        for index, node in enumerate(self.NODE_ORDER):
            widget = HealthNodeWidget(node)
            self.nodes[node] = widget
            chain.addWidget(widget, 1)
            if index < len(self.NODE_ORDER) - 1:
                arrow = QLabel("→")
                arrow.setObjectName("muted")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                chain.addWidget(arrow)
        layout.addLayout(chain)

    def set_testing(self) -> None:
        self.summary.setText("正在执行分层检测…")
        for widget in self.nodes.values():
            widget.set_state(HealthState.TESTING, "检测中")

    def set_snapshot(self, snapshot: HealthSnapshot) -> None:
        failed = 0
        degraded = 0
        for node, widget in self.nodes.items():
            result = snapshot.nodes.get(node)
            if result:
                widget.set_result(result)
                failed += result.state == HealthState.FAILED
                degraded += result.state == HealthState.DEGRADED
        if failed:
            self.summary.setText(f"发现 {failed} 个明确故障点")
        elif degraded:
            self.summary.setText(f"发现 {degraded} 个不稳定节点")
        else:
            self.summary.setText(f"检测完成 · {snapshot.completed_at}")


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class Worker(QRunnable):
    def __init__(self, function) -> None:
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.result.emit(self.function())
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class HostTreeWidget(QTreeWidget):
    order_changed = pyqtSignal(object)
    left_item_clicked = pyqtSignal(object)

    def mouseReleaseEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and item:
            self.left_item_clicked.emit(item)

    def dropEvent(self, event) -> None:
        dragged = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if not dragged or dragged.parent() is not None or (target and target.parent() is not None):
            event.ignore()
            return
        before = [
            self.topLevelItem(index).data(0, HOST_ID_ROLE)
            for index in range(self.topLevelItemCount())
        ]
        super().dropEvent(event)
        after = [
            self.topLevelItem(index).data(0, HOST_ID_ROLE)
            for index in range(self.topLevelItemCount())
        ]
        if event.isAccepted() and before != after:
            self.order_changed.emit(after)


class HostRow(QWidget):
    def __init__(self, host: HostConfig) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        layout.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        name = QLabel(host.display_name)
        name.setStyleSheet("font-weight: 600; font-size: 14px;")
        self.subtitle = QLabel(host.alias)
        self.subtitle.setObjectName("muted")
        texts.addWidget(name)
        texts.addWidget(self.subtitle)
        layout.addLayout(texts, 1)
        self.state = "stopped"
        self.update_state("stopped")

    def update_state(self, state: str) -> None:
        self.state = state
        color = STATE_COLORS.get(state, STATE_COLORS["stopped"])
        self.dot.setStyleSheet(f"background:{color}; border-radius:5px;")


class MainWindow(QMainWindow):
    tunnel_event = pyqtSignal(str, str, str)

    def __init__(self, start_enabled_now: bool = False) -> None:
        super().__init__()
        self.setWindowTitle(f"SSH 隧道助手 v{__version__}")
        self.resize(1160, 760)
        self.setMinimumSize(940, 650)
        self.store = StateStore()
        try:
            self.state = self.store.load()
        except Exception as exc:
            self.state = AppState()
            QMessageBox.warning(self, "配置读取失败", str(exc))
        self.log_listener = configure_logging(self.store.log_dir, self.state.settings)
        log_event(
            logger, logging.INFO, "app.started", version=__version__,
            hosts=len(self.state.hosts), config_read_only=self.store.read_only,
        )
        self.pool = QThreadPool.globalInstance()
        self.actions = HostActions(lambda: self.state.settings)
        self.health_service = HealthProbeService(self.actions)
        self.tunnels = TunnelManager(lambda: self.state.settings, self._thread_tunnel_event)
        self.rows: dict[str, HostRow] = {}
        self.health_snapshots: dict[str, HealthSnapshot] = {}
        self._health_running: set[str] = set()
        self._really_quit = False
        self.tunnel_event.connect(self._on_tunnel_event)
        self._build_ui()
        self._build_tray()
        self._review_migrated_hosts()
        for host in self.state.hosts:
            try:
                self.actions.configure_vscode_environment(host)
            except Exception as exc:
                self.append_log(f"{host.alias}: 本机 VSCode 端口映射失败：{exc}")
        if self.store.read_only:
            QMessageBox.warning(
                self, "配置来自更高版本",
                f"配置 schema {self.store.loaded_schema_version} 高于当前支持版本。\n"
                "本次会话不会覆盖保存配置。",
            )
        external_tunnels = self.tunnels.discover_existing(self.state.hosts)
        self.refresh_hosts()
        for alias in external_tunnels:
            self.append_log(f"{alias}: 检测到已运行隧道，由原助手窗口继续管理")
        self.external_tunnel_timer = QTimer(self)
        self.external_tunnel_timer.timeout.connect(self.tunnels.check_external)
        self.external_tunnel_timer.start(5000)
        QTimer.singleShot(900, self._install_vscode_bridge_background)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self.run_health_probe)
        self.health_timer.setInterval(max(15, self.state.settings.health_probe_interval) * 1000)
        if self.state.settings.automatic_health_checks:
            self.health_timer.start()
        if self.state.hosts and self.state.settings.automatic_health_checks:
            QTimer.singleShot(900, self.run_health_probe)
        pending = self.store.load_runtime().get("pending_update", {})
        if pending.get("resume_hosts"):
            QTimer.singleShot(700, self._resume_after_update)
        if start_enabled_now or self.state.settings.start_enabled_on_launch:
            QTimer.singleShot(450, self._auto_start)
        if self.state.settings.check_updates_on_launch:
            QTimer.singleShot(2500, lambda: self.check_updates(silent=True))

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(300)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 18, 16, 14)
        side.setSpacing(10)
        brand_row = QHBoxLayout()
        brand = QLabel("SSH 隧道助手")
        brand.setObjectName("brand")
        settings_button = QPushButton("⚙  设置")
        settings_button.setObjectName("settingsButton")
        settings_button.setMinimumWidth(82)
        settings_button.setToolTip("全局设置")
        settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_button.clicked.connect(self.open_settings)
        brand_row.addWidget(brand)
        brand_row.addStretch()
        brand_row.addWidget(settings_button)
        side.addLayout(brand_row)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索服务器或工作区")
        self.search.textChanged.connect(self.filter_hosts)
        side.addWidget(self.search)
        self.host_tree = HostTreeWidget()
        self.host_tree.setHeaderHidden(True)
        self.host_tree.setColumnCount(1)
        self.host_tree.setIndentation(18)
        self.host_tree.setRootIsDecorated(True)
        self.host_tree.setAnimated(True)
        self.host_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.host_tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.host_tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.host_tree.setDropIndicatorShown(True)
        self.host_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.host_tree.setToolTip("已连接服务器自动置顶；展开后可直接打开历史工作区，也可拖动调整组内顺序")
        self.host_tree.currentItemChanged.connect(self._selection_changed)
        self.host_tree.left_item_clicked.connect(
            lambda item: self._sidebar_item_clicked(item, 0)
        )
        self.host_tree.customContextMenuRequested.connect(self._sidebar_context_menu)
        self.host_tree.order_changed.connect(self._hosts_reordered)
        side.addWidget(self.host_tree, 1)
        side_buttons = QHBoxLayout()
        add_button = QPushButton("添加")
        import_button = QPushButton("从 SSH 导入")
        remove_button = QPushButton("删除")
        add_button.clicked.connect(self.add_host)
        import_button.clicked.connect(self.import_hosts)
        remove_button.clicked.connect(self.remove_host)
        side_buttons.addWidget(add_button)
        side_buttons.addWidget(import_button, 1)
        side_buttons.addWidget(remove_button)
        side.addLayout(side_buttons)
        root_layout.addWidget(sidebar)

        content = QWidget()
        content.setObjectName("content")
        main = QVBoxLayout(content)
        main.setContentsMargins(30, 26, 30, 24)
        main.setSpacing(16)
        header = QHBoxLayout()
        names = QVBoxLayout()
        self.title = QLabel("选择一台主机")
        self.title.setObjectName("pageTitle")
        self.subtitle = QLabel("从左侧选择主机后即可建立专用代理隧道")
        self.subtitle.setObjectName("muted")
        names.addWidget(self.title)
        names.addWidget(self.subtitle)
        header.addLayout(names)
        header.addStretch()
        self.status_badge = QLabel("未选择")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_badge.setMinimumWidth(76)
        self._set_badge("stopped", "未选择")
        header.addWidget(self.status_badge)
        edit_button = QPushButton("编辑")
        edit_button.clicked.connect(self.edit_host)
        header.addWidget(edit_button)
        main.addLayout(header)

        cards = QHBoxLayout()
        self.target_value = QLabel("—")
        self.forward_value = QLabel("—")
        self.reconnect_value = QLabel("—")
        cards.addWidget(self._info_card("SSH 目标", self.target_value), 1)
        cards.addWidget(self._info_card("代理转发", self.forward_value), 1)
        cards.addWidget(self._info_card("自动恢复", self.reconnect_value), 1)
        main.addLayout(cards)

        self.health_chain = HealthChainWidget()
        main.addWidget(self.health_chain)

        action_card = self._card()
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(20, 18, 20, 18)
        action_layout.setSpacing(12)
        section = QLabel("连接与使用")
        section.setObjectName("sectionTitle")
        action_layout.addWidget(section)
        primary_row = QHBoxLayout()
        self.start_button = QPushButton("启动隧道")
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("danger")
        self.start_button.clicked.connect(self.start_selected)
        self.stop_button.clicked.connect(self.stop_selected)
        primary_row.addWidget(self.start_button)
        primary_row.addWidget(self.stop_button)
        primary_row.addStretch()
        action_layout.addLayout(primary_row)
        launch_row = QHBoxLayout()
        terminal = QPushButton("打开 SSH 终端")
        vscode = QPushButton("用 VSCode 打开")
        codex = QPushButton("打开远程 Codex")
        terminal.clicked.connect(lambda: self._launch("terminal"))
        vscode.clicked.connect(lambda: self._launch("vscode"))
        codex.clicked.connect(lambda: self._launch("codex"))
        launch_row.addWidget(terminal)
        launch_row.addWidget(vscode)
        launch_row.addWidget(codex)
        launch_row.addStretch()
        action_layout.addLayout(launch_row)
        main.addWidget(action_card)

        test_card = self._card()
        test_layout = QVBoxLayout(test_card)
        test_layout.setContentsMargins(20, 16, 20, 16)
        test_layout.setSpacing(10)
        test_title = QHBoxLayout()
        label = QLabel("检测与修复")
        label.setObjectName("sectionTitle")
        test_title.addWidget(label)
        test_title.addStretch()
        deep = QPushButton("深度诊断")
        deep.setObjectName("primary")
        deep.clicked.connect(lambda: self.run_health_probe(show_details=True))
        test_title.addWidget(deep)
        repair = QPushButton("修复首个故障")
        repair.clicked.connect(self.repair_chain)
        test_title.addWidget(repair)
        persistent_logs = QPushButton("持久日志")
        persistent_logs.setObjectName("ghost")
        persistent_logs.clicked.connect(self.show_persistent_logs)
        test_title.addWidget(persistent_logs)
        bundle = QPushButton("导出诊断包")
        bundle.setObjectName("ghost")
        bundle.clicked.connect(self.export_diagnostics)
        test_title.addWidget(bundle)
        clear = QPushButton("清空日志")
        clear.setObjectName("ghost")
        clear.clicked.connect(lambda: self.log.clear())
        test_title.addWidget(clear)
        test_layout.addLayout(test_title)
        tests = QHBoxLayout()
        for text, callback in [
            ("测试本地代理", self.test_local), ("测试 SSH", self.test_ssh),
            ("测试远程代理", self.test_remote), ("安装 VSCode 组件", self.configure_vscode_bridge),
            ("Codex 冒烟测试", self.smoke_codex),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            tests.addWidget(button)
        tests.addStretch()
        test_layout.addLayout(tests)
        external_logs = QHBoxLayout()
        for text, callback in [
            ("查看 Codex 日志", self.show_codex_logs),
            ("打开 VSCode 本机日志", self.open_vscode_logs),
            ("查看远程 VSCode 日志", self.show_remote_vscode_logs),
            ("检查更新", lambda: self.check_updates(silent=False)),
        ]:
            button = QPushButton(text)
            button.setObjectName("ghost")
            button.clicked.connect(callback)
            external_logs.addWidget(button)
        external_logs.addStretch()
        test_layout.addLayout(external_logs)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(130)
        test_layout.addWidget(self.log, 1)
        main.addWidget(test_card, 1)
        root_layout.addWidget(content, 1)

    @staticmethod
    def _card() -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        return card

    def _info_card(self, caption: str, value: QLabel) -> QFrame:
        card = self._card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 15)
        label = QLabel(caption)
        label.setObjectName("muted")
        value.setStyleSheet("font-size: 15px; font-weight: 600;")
        value.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(value)
        return card

    def _build_tray(self) -> None:
        icon = QIcon(str(resource_path("assets/logo.png")))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("SSH 隧道助手")
        menu = QMenu()
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self._show_window)
        start_all = QAction("启动自动主机", self)
        start_all.triggered.connect(self._auto_start)
        stop_all = QAction("停止全部隧道", self)
        stop_all.triggered.connect(self.tunnels.stop_all)
        update_action = QAction("检查更新", self)
        update_action.triggered.connect(lambda: self.check_updates(silent=False))
        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self._quit)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(start_all)
        menu.addAction(stop_all)
        menu.addAction(update_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def _import_if_empty(self) -> None:
        """Kept as a compatibility hook; imports are now always user initiated."""
        return

    def _review_migrated_hosts(self) -> None:
        if not self.state.extra.get("migration_v2_review_pending"):
            return
        migrated_count = len(self.state.hosts)
        self.state.onboarding_completed = True
        self.state.extra.pop("migration_v2_review_pending", None)
        if self._save_state():
            self.append_log(
                f"配置已升级并保留原有 {migrated_count} 台主机；迁移前配置已自动备份"
            )

    def _do_import(self, silent: bool = False) -> int:
        aliases = parse_host_aliases(self.state.settings.ssh_config_path)
        existing = {host.alias.lower() for host in self.state.hosts}
        added = 0
        for alias in aliases:
            if alias.lower() not in existing:
                self.state.hosts.append(HostConfig(
                    alias=alias,
                    remote_proxy_port=self.state.settings.default_remote_proxy_port,
                    source="ssh_import",
                ))
                existing.add(alias.lower())
                added += 1
        self.state.onboarding_completed = True
        self._save_state()
        if not silent:
            self.append_log(f"已从 SSH 配置导入 {added} 台新主机，共 {len(self.state.hosts)} 台")
        return added

    def import_hosts(self) -> None:
        try:
            self._do_import()
            self.refresh_hosts()
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))

    def refresh_hosts(self, select_alias: str | None = None) -> None:
        current = select_alias or (self.selected_host().alias if self.selected_host() else None)
        expanded_ids = {
            self.host_tree.topLevelItem(index).data(0, HOST_ID_ROLE)
            for index in range(self.host_tree.topLevelItemCount())
            if self.host_tree.topLevelItem(index).isExpanded()
        }
        self.host_tree.clear()
        self.rows.clear()
        folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirLinkIcon)
        connected_aliases = {
            host.alias
            for host in self.state.hosts
            if self.tunnels.runtime(host.alias).state == TunnelState.CONNECTED
        }
        for host in connected_hosts_first(self.state.hosts, connected_aliases):
            item = QTreeWidgetItem()
            item.setData(0, ITEM_KIND_ROLE, "host")
            item.setData(0, HOST_ID_ROLE, host.id)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            item.setSizeHint(0, QSize(250, 58))
            row = HostRow(host)
            state = self.tunnels.runtime(host.alias).state.value
            row.update_state(state)
            self.rows[host.alias] = row
            self.host_tree.addTopLevelItem(item)
            self.host_tree.setItemWidget(item, 0, row)
            for workspace in host.workspace_shortcuts():
                is_default = workspace == host.remote_dir
                child = QTreeWidgetItem(item)
                child.setText(0, f"{'★  ' if is_default else ''}{workspace}")
                child.setIcon(0, folder_icon)
                child.setData(0, ITEM_KIND_ROLE, "workspace")
                child.setData(0, HOST_ID_ROLE, host.id)
                child.setData(0, WORKSPACE_PATH_ROLE, workspace)
                child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                child.setSizeHint(0, QSize(225, 34))
                child.setToolTip(
                    0, f"单击直接用 VSCode 打开\n{workspace}"
                    + ("\n★ 默认工作区" if is_default else "")
                )
            item.setExpanded(host.id in expanded_ids)
            if current == host.alias:
                self.host_tree.setCurrentItem(item)
        if not self.state.hosts:
            empty = QTreeWidgetItem(["暂无服务器\n请添加或从 SSH config 导入"])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setTextAlignment(0, Qt.AlignmentFlag.AlignCenter)
            empty.setSizeHint(0, QSize(250, 90))
            self.host_tree.addTopLevelItem(empty)
        if self.host_tree.currentItem() is None and self.state.hosts:
            self.host_tree.setCurrentItem(self.host_tree.topLevelItem(0))
        self.filter_hosts(self.search.text())

    def filter_hosts(self, text: str) -> None:
        needle = text.strip().lower()
        self.host_tree.setDragDropMode(
            QAbstractItemView.DragDropMode.NoDragDrop
            if needle else QAbstractItemView.DragDropMode.InternalMove
        )
        for index in range(self.host_tree.topLevelItemCount()):
            item = self.host_tree.topLevelItem(index)
            host = self._host_by_id(item.data(0, HOST_ID_ROLE))
            if not host:
                continue
            host_matches = needle in f"{host.alias} {host.display_name}".lower()
            child_matches = False
            for child_index in range(item.childCount()):
                child = item.child(child_index)
                path_matches = needle in str(child.data(0, WORKSPACE_PATH_ROLE)).lower()
                child.setHidden(bool(needle and not host_matches and not path_matches))
                child_matches = child_matches or path_matches
            item.setHidden(bool(needle and not host_matches and not child_matches))
            if needle and child_matches:
                item.setExpanded(True)

    def selected_host(self) -> HostConfig | None:
        item = self.host_tree.currentItem() if hasattr(self, "host_tree") else None
        return self._host_by_id(item.data(0, HOST_ID_ROLE)) if item else None

    def _host(self, alias: str) -> HostConfig | None:
        return next((host for host in self.state.hosts if host.alias == alias), None)

    def _host_by_id(self, host_id: str | None) -> HostConfig | None:
        return next((host for host in self.state.hosts if host.id == host_id), None)

    def _hosts_reordered(self, host_ids: list[str]) -> None:
        by_id = {host.id: host for host in self.state.hosts}
        if len(host_ids) != len(by_id) or set(host_ids) != set(by_id):
            self.refresh_hosts()
            return
        self.state.hosts = [by_id[host_id] for host_id in host_ids]
        if self._save_state():
            self.append_log("服务器顺序已保存")
            selected = self.selected_host()
            QTimer.singleShot(
                0, lambda alias=selected.alias if selected else None: self.refresh_hosts(alias)
            )

    def _sidebar_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if item.data(0, ITEM_KIND_ROLE) != "workspace":
            return
        host = self._host_by_id(item.data(0, HOST_ID_ROLE))
        workspace = item.data(0, WORKSPACE_PATH_ROLE)
        if host and workspace:
            self._launch_vscode_path(host, workspace)

    def _sidebar_context_menu(self, point) -> None:
        item = self.host_tree.itemAt(point)
        if not item:
            return
        host = self._host_by_id(item.data(0, HOST_ID_ROLE))
        if not host:
            return
        self.host_tree.setCurrentItem(item)
        menu = QMenu(self)
        if item.data(0, ITEM_KIND_ROLE) == "workspace":
            workspace = item.data(0, WORKSPACE_PATH_ROLE)
            open_action = menu.addAction("用 VSCode 打开")
            default_action = menu.addAction("设为默认工作区")
            menu.addSeparator()
            forget_action = menu.addAction("从历史记录移除")
            forget_action.setEnabled(workspace != host.remote_dir)
            chosen = menu.exec(self.host_tree.viewport().mapToGlobal(point))
            if chosen == open_action:
                self._launch_vscode_path(host, workspace)
            elif chosen == default_action:
                host.remote_dir = workspace
                host.remember_workspace(workspace)
                self._save_state()
                self.refresh_hosts(host.alias)
            elif chosen == forget_action:
                host.forget_workspace(workspace)
                self._save_state()
                self.refresh_hosts(host.alias)
            return
        toggle_action = menu.addAction("收起工作区" if item.isExpanded() else "展开工作区")
        open_action = menu.addAction("用 VSCode 打开默认工作区")
        menu.addSeparator()
        edit_action = menu.addAction("编辑服务器")
        remove_action = menu.addAction("从软件列表删除")
        chosen = menu.exec(self.host_tree.viewport().mapToGlobal(point))
        if chosen == toggle_action:
            item.setExpanded(not item.isExpanded())
        elif chosen == open_action:
            self._launch_vscode_path(host, host.remote_dir or "~")
        elif chosen == edit_action:
            self.edit_host()
        elif chosen == remove_action:
            self.remove_host()

    def _selection_changed(self, current, previous) -> None:
        host = self.selected_host()
        if not host:
            return
        self.title.setText(host.display_name)
        self.subtitle.setText(host.alias)
        self.forward_value.setText(f"127.0.0.1:{host.remote_proxy_port}  ←  本机 {self.state.settings.local_proxy_host}:{self.state.settings.local_proxy_port}")
        self.reconnect_value.setText("已开启" if host.auto_reconnect else "已关闭")
        runtime = self.tunnels.runtime(host.alias)
        self._set_badge(runtime.state.value)
        cached = self.health_snapshots.get(host.id)
        if cached:
            self.health_chain.set_snapshot(cached)
        else:
            QTimer.singleShot(150, self.run_health_probe)
        self.target_value.setText("正在读取…")
        self._run_async("解析 SSH 配置", lambda: resolve_host(
            self.state.settings.ssh_path, self.state.settings.ssh_config_path,
            host.alias, self.state.settings.connect_timeout
        ), self._resolved)

    def _resolved(self, resolved) -> None:
        host = self.selected_host()
        if not host or resolved.alias != host.alias:
            return
        target = f"{resolved.user + '@' if resolved.user else ''}{resolved.hostname}:{resolved.port}"
        if resolved.proxy_jump and resolved.proxy_jump.lower() != "none":
            target += f"  ·  经 {resolved.proxy_jump}"
        self.target_value.setText(target)
        if resolved.configured_remote_forwards:
            self.append_log(
                f"{host.alias}: SSH config 自带 RemoteForward，会由专用隧道一并建立；"
                "建议删除旧转发并改用本软件的远程端口，避免额外端口冲突"
            )

    def add_host(self) -> None:
        dialog = HostDialog(self, default_port=self.state.settings.default_remote_proxy_port)
        if dialog.exec():
            host = dialog.value()
            if self._host(host.alias):
                QMessageBox.warning(self, "主机已存在", "这个 SSH 别名已经在列表中。")
                return
            entry = dialog.ssh_entry()
            if entry:
                try:
                    backup = append_host_entry(self.state.settings.ssh_config_path, entry)
                except Exception as exc:
                    QMessageBox.warning(self, "SSH 配置写入失败", str(exc))
                    return
                backup_text = f"；备份：{backup}" if backup else ""
                self.append_log(f"已将 Host {entry.alias} 写入 SSH config{backup_text}")
            self.state.hosts.append(host)
            self.state.onboarding_completed = True
            self._save_state()
            try:
                mapping = self.actions.configure_vscode_environment(host)
                self.append_log(f"已为 {host.alias} 保存 VSCode 代理端口：{mapping}")
            except Exception as exc:
                QMessageBox.warning(self, "代理端口配置失败", str(exc))
            self.refresh_hosts(host.alias)

    def edit_host(self) -> None:
        host = self.selected_host()
        if not host:
            return
        old_alias = host.alias
        dialog = HostDialog(self, host, self.state.settings.default_remote_proxy_port)
        if dialog.exec():
            updated = dialog.value()
            if updated.alias != old_alias and self._host(updated.alias):
                QMessageBox.warning(self, "主机已存在", "这个 SSH 别名已经在列表中。")
                return
            self.tunnels.stop(old_alias)
            index = self.state.hosts.index(host)
            self.state.hosts[index] = updated
            self._save_state()
            try:
                mapping = self.actions.configure_vscode_environment(updated)
                self.append_log(f"已为 {updated.alias} 更新 VSCode 代理端口：{mapping}")
            except Exception as exc:
                QMessageBox.warning(self, "代理端口配置失败", str(exc))
            self.refresh_hosts(updated.alias)

    def remove_host(self) -> None:
        host = self.selected_host()
        if not host:
            return
        answer = QMessageBox.question(
            self, "移除主机", f"从软件列表中移除“{host.display_name}”？\n不会修改 SSH config。"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.tunnels.stop(host.alias)
            self.state.hosts.remove(host)
            self._save_state()
            self.refresh_hosts()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.state.settings, self)
        if dialog.exec():
            dialog.apply_to(self.state.settings)
            self._save_state()
            self.health_timer.setInterval(max(15, self.state.settings.health_probe_interval) * 1000)
            if self.state.settings.automatic_health_checks:
                self.health_timer.start()
            else:
                self.health_timer.stop()
            self.append_log("全局设置已保存；已运行的隧道会在下次重连时使用新设置")
            self._selection_changed(self.host_tree.currentItem(), None)

    def start_selected(self) -> None:
        host = self.selected_host()
        if not host:
            return
        self.append_log(f"{host.alias}: 检查本地代理…")
        self._run_async("本地代理检查", self.actions.test_local_proxy, lambda result: self._start_after_proxy(host.alias, result))

    def _start_after_proxy(self, alias: str, result: ActionResult) -> None:
        self._show_result(result)
        host = self._host(alias)
        if result.ok and host:
            self.tunnels.start(host)

    def stop_selected(self) -> None:
        host = self.selected_host()
        if host:
            self.tunnels.stop(host.alias)

    def test_local(self) -> None:
        self._action("测试本地代理", self.actions.test_local_proxy)

    def test_ssh(self) -> None:
        host = self.selected_host()
        if host:
            self._action("测试 SSH", lambda: self.actions.test_ssh(host))

    def test_remote(self) -> None:
        host = self.selected_host()
        if host:
            self._action("测试远程代理", lambda: self.actions.test_remote_proxy(host))

    def configure_vscode_bridge(self) -> None:
        host = self.selected_host()
        if host:
            try:
                mapping = self.actions.configure_vscode_environment(host)
                self.append_log(f"{host.alias}: 已更新本机 VSCode 端口映射：{mapping}")
            except Exception as exc:
                QMessageBox.warning(self, "VSCode 配置失败", str(exc))
                return
        self._action("安装 VSCode 配套组件", self.actions.install_vscode_bridge)

    def _install_vscode_bridge_background(self) -> None:
        self._run_async(
            "检查 VSCode 配套组件",
            self.actions.install_vscode_bridge,
            self._show_result,
        )

    def smoke_codex(self) -> None:
        host = self.selected_host()
        if host:
            self._action("Codex 冒烟测试", lambda: self.actions.smoke_codex(host))

    def _action(self, label: str, function) -> None:
        self.append_log(f"{label}…")
        self._run_async(label, function, self._show_result)

    def _show_result(self, result: ActionResult) -> None:
        marker = "成功" if result.ok else "失败"
        self.append_log(f"[{marker}] {result.title}: {result.detail or '完成'}")

    def _launch(self, kind: str) -> None:
        host = self.selected_host()
        if not host:
            return
        if kind == "vscode":
            self._launch_vscode_workspace(host)
            return
        try:
            {"terminal": self.actions.launch_terminal, "vscode": self.actions.launch_vscode, "codex": self.actions.launch_codex}[kind](host)
            self.append_log(f"已为 {host.alias} 打开 {kind}")
        except Exception as exc:
            QMessageBox.warning(self, "启动失败", str(exc))

    def _launch_vscode_workspace(self, host: HostConfig) -> None:
        dialog = RemoteFolderDialog(host, self.actions, self)
        if not dialog.exec():
            return
        workspace = dialog.path()
        self._launch_vscode_path(host, workspace, dialog.remember.isChecked())

    def _launch_vscode_path(
        self, host: HostConfig, workspace: str, remember_default: bool = False
    ) -> None:
        workspace = workspace.strip() or "~"
        try:
            self.actions.launch_vscode(replace(host, remote_dir=workspace))
            host.remember_workspace(workspace)
            if remember_default:
                host.remote_dir = workspace
            self._save_state()
            self.append_log(
                f"已用 VSCode 打开 {host.alias}:{workspace}；新终端固定使用远程端口 "
                f"{host.remote_proxy_port}"
            )
            self.refresh_hosts(host.alias)
        except Exception as exc:
            QMessageBox.warning(self, "启动失败", str(exc))

    def _save_state(self) -> bool:
        try:
            self.store.save(self.state)
            return True
        except UnsupportedConfigVersion as exc:
            QMessageBox.warning(self, "配置为只读", str(exc))
        except Exception as exc:
            QMessageBox.warning(self, "配置保存失败", str(exc))
            log_event(logger, logging.ERROR, "config.save_failed", error=str(exc))
        return False

    def run_health_probe(self, show_details: bool = False) -> None:
        host = self.selected_host()
        if not host or host.id in self._health_running:
            return
        self._health_running.add(host.id)
        self.health_chain.set_testing()
        state = self.tunnels.runtime(host.alias).state.value
        self.append_log(f"{host.alias}: 开始分层链路检测…")
        worker = Worker(lambda: self.health_service.run_full(host, state))
        worker.signals.result.connect(
            lambda snapshot: self._health_completed(snapshot, show_details)
        )
        worker.signals.error.connect(
            lambda error: self.append_log(f"[失败] 链路检测: {error.splitlines()[-1]}")
        )
        worker.signals.finished.connect(lambda: self._health_running.discard(host.id))
        self.pool.start(worker)

    def _health_completed(self, snapshot: HealthSnapshot, show_details: bool) -> None:
        self.health_snapshots[snapshot.host_id] = snapshot
        host = self.selected_host()
        if host and host.id == snapshot.host_id:
            self.health_chain.set_snapshot(snapshot)
        failures = [value for value in snapshot.nodes.values() if value.state == HealthState.FAILED]
        marker = "失败" if failures else "完成"
        self.append_log(f"{snapshot.host_alias}: 链路检测{marker}，明确故障点 {len(failures)} 个")
        if show_details:
            lines = [f"主机：{snapshot.host_alias}", f"完成时间：{snapshot.completed_at}", ""]
            for node in HealthChainWidget.NODE_ORDER:
                result = snapshot.nodes.get(node)
                if not result:
                    continue
                lines.extend([
                    f"[{result.state.value}] {HEALTH_NODE_LABELS[node]} — {result.title}",
                    f"耗时：{result.duration_ms} ms",
                    f"最后成功：{result.last_success_at or '无'}",
                    f"最后失败：{result.last_failure_at or '无'}",
                    f"连续失败：{result.consecutive_failures} 次",
                    result.detail or "无详细信息",
                    "",
                ])
            TextViewerDialog("链路深度诊断", "\n".join(lines), self).exec()

    def repair_chain(self) -> None:
        host = self.selected_host()
        if not host:
            return
        snapshot = self.health_snapshots.get(host.id)
        if not snapshot:
            QMessageBox.information(self, "尚未诊断", "请先运行“深度诊断”。")
            return
        failed = [
            node for node in HealthChainWidget.NODE_ORDER
            if snapshot.nodes.get(node) and snapshot.nodes[node].state == HealthState.FAILED
        ]
        if not failed:
            QMessageBox.information(self, "没有明确故障", "当前结果没有可自动修复的明确故障点。")
            return
        node = failed[0]
        if node in {"ssh_tunnel", "remote_port"}:
            answer = QMessageBox.question(
                self, "重新建立隧道",
                f"首个故障点是“{HEALTH_NODE_LABELS[node]}”。\n是否停止并重新建立 {host.alias} 的隧道？",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.tunnels.stop(host.alias)
                QTimer.singleShot(350, lambda: self.tunnels.start(host))
                QTimer.singleShot(1800, self.run_health_probe)
            return
        if node == "codex":
            QMessageBox.information(
                self, "Codex 修复建议",
                "请确认远程已安装最新版 Codex，并查看 Codex 日志。软件不会自动修改远程安装。",
            )
            self.show_codex_logs()
            return
        if node in {"local_proxy", "clash_node", "openai"}:
            answer = QMessageBox.question(
                self, "代理修复建议",
                f"首个故障点是“{HEALTH_NODE_LABELS[node]}”。\n"
                "请确认 Clash 正在运行、端口和 Controller 设置正确，必要时切换节点。\n\n是否打开全局设置？",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.open_settings()

    def show_persistent_logs(self) -> None:
        StructuredLogDialog(self.store.log_dir, self).exec()

    def export_diagnostics(self) -> None:
        try:
            path = create_diagnostic_bundle(self.store, self.state, self.health_snapshots)
            self.append_log(f"诊断包已生成：{path}")
            answer = QMessageBox.question(
                self, "诊断包已生成", f"已生成：\n{path}\n\n是否打开所在文件夹？"
            )
            if answer == QMessageBox.StandardButton.Yes:
                os.startfile(path.parent)  # type: ignore[attr-defined]
        except Exception as exc:
            QMessageBox.warning(self, "诊断包生成失败", str(exc))

    def _show_external_log(self, result: ActionResult) -> None:
        self._show_result(result)
        if result.ok:
            TextViewerDialog(result.title, result.detail, self).exec()

    def show_codex_logs(self) -> None:
        host = self.selected_host()
        if host:
            self._run_async(
                "读取 Codex 日志", lambda: self.actions.latest_codex_log(host),
                self._show_external_log,
            )

    def open_vscode_logs(self) -> None:
        try:
            path = self.actions.open_vscode_logs()
            self.append_log(f"已打开 VSCode 日志目录：{path}")
        except Exception as exc:
            QMessageBox.warning(
                self, "未找到 VSCode 日志",
                f"{exc}\n\n也可以在 VSCode 命令面板运行 Remote-SSH: Show Log。",
            )

    def show_remote_vscode_logs(self) -> None:
        host = self.selected_host()
        if host:
            self._run_async(
                "读取远程 VSCode 日志",
                lambda: self.actions.latest_remote_vscode_log(host),
                self._show_external_log,
            )

    def check_updates(self, silent: bool = False) -> None:
        repository = self.state.settings.update_repository
        self._run_async(
            "检查更新", lambda: check_for_update(repository, __version__),
            lambda info: self._update_checked(info, silent),
        )

    def _update_checked(self, info: UpdateInfo | None, silent: bool) -> None:
        if info is None:
            if not silent:
                QMessageBox.information(self, "检查更新", f"当前 v{__version__} 已是最新版本。")
            return
        notes = info.notes.strip()
        if len(notes) > 1200:
            notes = notes[:1200] + "\n…"
        answer = QMessageBox.question(
            self, "发现新版本",
            f"当前版本：v{__version__}\n最新版本：v{info.version}\n\n{notes or '暂无更新说明'}\n\n"
            "是否下载并安装？更新时会短暂停止由本窗口管理的隧道。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.append_log(f"开始下载更新 v{info.version}…")
            self._run_async(
                "下载更新", lambda: download_update(info, self.store.update_dir),
                lambda path: self._install_update(info, path),
            )

    def _install_update(self, info: UpdateInfo, path: Path) -> None:
        resume_hosts = [
            host.alias for host in self.state.hosts
            if self.tunnels.runtime(host.alias).process
            and self.tunnels.runtime(host.alias).process.poll() is None
        ]
        runtime = self.store.load_runtime()
        runtime["pending_update"] = {
            "from_version": __version__,
            "to_version": info.version,
            "resume_hosts": resume_hosts,
            "installer": str(path),
        }
        self.store.save_runtime(runtime)
        self.append_log("更新已下载，正在安全停止隧道并启动安装器…")
        self.tunnels.stop_all()
        launch_installer(path)
        self._really_quit = True
        log_event(logger, logging.INFO, "app.exit_for_update", target_version=info.version)
        shutdown_logging()
        QApplication.quit()

    def _resume_after_update(self) -> None:
        runtime = self.store.load_runtime()
        pending = runtime.get("pending_update")
        if not isinstance(pending, dict):
            return
        aliases = pending.get("resume_hosts") if isinstance(pending.get("resume_hosts"), list) else []
        for alias in aliases:
            host = self._host(str(alias))
            if host:
                self.tunnels.start(host)
        runtime.pop("pending_update", None)
        self.store.save_runtime(runtime)
        if aliases:
            self.append_log(f"更新完成，正在恢复 {len(aliases)} 条隧道")

    def _run_async(self, label: str, function, callback=None) -> None:
        worker = Worker(function)
        if callback:
            worker.signals.result.connect(callback)
        worker.signals.error.connect(lambda error: self.append_log(f"[失败] {label}: {error.splitlines()[-1]}"))
        self.pool.start(worker)

    def _thread_tunnel_event(self, alias: str, state: TunnelState, message: str) -> None:
        self.tunnel_event.emit(alias, state.value, message)

    def _on_tunnel_event(self, alias: str, state: str, message: str) -> None:
        row = self.rows.get(alias)
        was_connected = bool(row and row.state == TunnelState.CONNECTED.value)
        if row:
            row.update_state(state)
        host = self.selected_host()
        if host and host.alias == alias:
            self._set_badge(state)
        self.append_log(f"{alias}: {message}")
        if was_connected != (state == TunnelState.CONNECTED.value):
            QTimer.singleShot(0, self.refresh_hosts)
            if host and host.alias == alias and self.state.settings.automatic_health_checks:
                QTimer.singleShot(500, self.run_health_probe)

    def _set_badge(self, state: str, text: str | None = None) -> None:
        color = STATE_COLORS.get(state, STATE_COLORS["stopped"])
        background = STATE_BACKGROUNDS.get(state, STATE_BACKGROUNDS["stopped"])
        self.status_badge.setText(text or STATE_TEXT.get(state, state))
        self.status_badge.setStyleSheet(f"color:{color}; background:{background}; border:1px solid {color}; border-radius:12px; padding:5px 10px; font-weight:600;")

    def append_log(self, message: str) -> None:
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")
        log_event(logger, logging.INFO, "ui.message", message=message)

    def _auto_start(self) -> None:
        for host in self.state.hosts:
            if host.enabled:
                self.tunnels.start(host)

    def _show_window(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _quit(self) -> None:
        self._really_quit = True
        self.tunnels.stop_all()
        log_event(logger, logging.INFO, "app.exiting")
        shutdown_logging()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.state.settings.minimize_to_tray and not self._really_quit:
            event.ignore()
            self.hide()
            self.tray.showMessage("SSH 隧道助手", "窗口已隐藏，隧道会继续运行。", QSystemTrayIcon.MessageIcon.Information, 2500)
        else:
            self.tunnels.stop_all()
            shutdown_logging()
            event.accept()
