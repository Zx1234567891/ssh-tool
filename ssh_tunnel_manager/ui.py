from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from pathlib import Path
import traceback

from PyQt6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .actions import ActionResult, HostActions
from .dialogs import HostDialog, RemoteFolderDialog, SettingsDialog
from .models import AppState, HostConfig, connected_hosts_first
from .resources import resource_path
from .ssh_config import append_host_entry, parse_host_aliases, resolve_host
from .store import StateStore
from .theme import STATE_BACKGROUNDS, STATE_COLORS, STATE_TEXT
from .tunnel import TunnelManager, TunnelState


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

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SSH 隧道助手")
        self.resize(1160, 760)
        self.setMinimumSize(940, 650)
        self.store = StateStore()
        try:
            self.state = self.store.load()
        except Exception as exc:
            self.state = AppState()
            QMessageBox.warning(self, "配置读取失败", str(exc))
        self.pool = QThreadPool.globalInstance()
        self.actions = HostActions(lambda: self.state.settings)
        self.tunnels = TunnelManager(lambda: self.state.settings, self._thread_tunnel_event)
        self.rows: dict[str, HostRow] = {}
        self._really_quit = False
        self.tunnel_event.connect(self._on_tunnel_event)
        self._build_ui()
        self._build_tray()
        self._import_if_empty()
        self.refresh_hosts()
        QTimer.singleShot(450, self._auto_start)

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
        self.search.setPlaceholderText("搜索主机")
        self.search.textChanged.connect(self.filter_hosts)
        side.addWidget(self.search)
        self.host_list = QListWidget()
        self.host_list.setSpacing(2)
        self.host_list.currentItemChanged.connect(self._selection_changed)
        side.addWidget(self.host_list, 1)
        side_buttons = QHBoxLayout()
        add_button = QPushButton("添加")
        import_button = QPushButton("从 SSH 导入")
        remove_button = QPushButton("移除")
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
        clear = QPushButton("清空日志")
        clear.setObjectName("ghost")
        clear.clicked.connect(lambda: self.log.clear())
        test_title.addWidget(clear)
        test_layout.addLayout(test_title)
        tests = QHBoxLayout()
        for text, callback in [
            ("测试本地代理", self.test_local), ("测试 SSH", self.test_ssh),
            ("测试远程代理", self.test_remote), ("安装代理切换器", self.configure_remote),
            ("Codex 冒烟测试", self.smoke_codex),
        ]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            tests.addWidget(button)
        tests.addStretch()
        test_layout.addLayout(tests)
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
        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self._quit)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(start_all)
        menu.addAction(stop_all)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()

    def _import_if_empty(self) -> None:
        if not self.state.hosts:
            self._do_import(silent=True)

    def _do_import(self, silent: bool = False) -> int:
        aliases = parse_host_aliases(self.state.settings.ssh_config_path)
        existing = {host.alias.lower() for host in self.state.hosts}
        added = 0
        for alias in aliases:
            if alias.lower() not in existing:
                self.state.hosts.append(HostConfig(alias=alias, remote_proxy_port=self.state.settings.default_remote_proxy_port))
                existing.add(alias.lower())
                added += 1
        self.store.save(self.state)
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
        self.host_list.clear()
        self.rows.clear()
        connected_aliases = {
            host.alias for host in self.state.hosts
            if self.tunnels.runtime(host.alias).state == TunnelState.CONNECTED
        }
        for host in connected_hosts_first(self.state.hosts, connected_aliases):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, host.alias)
            item.setSizeHint(QSize(250, 58))
            row = HostRow(host)
            state = self.tunnels.runtime(host.alias).state.value
            row.update_state(state)
            self.rows[host.alias] = row
            self.host_list.addItem(item)
            self.host_list.setItemWidget(item, row)
            if current == host.alias:
                self.host_list.setCurrentItem(item)
        if self.host_list.currentItem() is None and self.host_list.count():
            self.host_list.setCurrentRow(0)
        self.filter_hosts(self.search.text())

    def filter_hosts(self, text: str) -> None:
        needle = text.strip().lower()
        for index in range(self.host_list.count()):
            item = self.host_list.item(index)
            alias = item.data(Qt.ItemDataRole.UserRole)
            host = self._host(alias)
            item.setHidden(bool(needle and host and needle not in f"{host.alias} {host.display_name}".lower()))

    def selected_host(self) -> HostConfig | None:
        item = self.host_list.currentItem() if hasattr(self, "host_list") else None
        return self._host(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _host(self, alias: str) -> HostConfig | None:
        return next((host for host in self.state.hosts if host.alias == alias), None)

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
            self.store.save(self.state)
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
            self.store.save(self.state)
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
            self.store.save(self.state)
            self.refresh_hosts()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.state.settings, self)
        if dialog.exec():
            dialog.apply_to(self.state.settings)
            self.store.save(self.state)
            self.append_log("全局设置已保存；已运行的隧道会在下次重连时使用新设置")
            self._selection_changed(self.host_list.currentItem(), None)

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

    def configure_remote(self) -> None:
        host = self.selected_host()
        if not host:
            return
        answer = QMessageBox.question(
            self, "安装代理切换器",
            "将备份远程 ~/.bashrc，移除旧的固定代理端口，并安装 stm_proxy_use/stm_proxy_off 命令。\n"
            "从软件打开 SSH、VSCode 或 Codex 时会自动选择本机对应端口。是否继续？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._action("安装代理切换器", lambda: self.actions.configure_remote_shell(host))

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
        host.workspaces = [workspace, *[item for item in host.workspaces if item != workspace]][:10]
        if dialog.remember.isChecked():
            host.remote_dir = workspace
        self.store.save(self.state)
        try:
            self.actions.launch_vscode(replace(host, remote_dir=workspace))
            self.append_log(
                f"已用 VSCode 打开 {host.alias}:{workspace}；新终端固定使用远程端口 "
                f"{host.remote_proxy_port}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "启动失败", str(exc))

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

    def _set_badge(self, state: str, text: str | None = None) -> None:
        color = STATE_COLORS.get(state, STATE_COLORS["stopped"])
        background = STATE_BACKGROUNDS.get(state, STATE_BACKGROUNDS["stopped"])
        self.status_badge.setText(text or STATE_TEXT.get(state, state))
        self.status_badge.setStyleSheet(f"color:{color}; background:{background}; border:1px solid {color}; border-radius:12px; padding:5px 10px; font-weight:600;")

    def append_log(self, message: str) -> None:
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

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
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.state.settings.minimize_to_tray and not self._really_quit:
            event.ignore()
            self.hide()
            self.tray.showMessage("SSH 隧道助手", "窗口已隐藏，隧道会继续运行。", QSystemTrayIcon.MessageIcon.Information, 2500)
        else:
            self.tunnels.stop_all()
            event.accept()
