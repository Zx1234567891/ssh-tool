from __future__ import annotations

import threading
import json
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QStyle, QVBoxLayout, QWidget,
)

from .actions import HostActions, RemoteDirectoryListing
from .models import AppSettings, HostConfig, utc_now
from .ssh_config import SshHostEntry


class HostDialog(QDialog):
    def __init__(self, parent=None, host: HostConfig | None = None, default_port: int = 10099) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑主机" if host else "添加主机")
        self.setMinimumWidth(520)
        self._host = host
        self._workspaces = list(host.workspaces) if host else []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        title = QLabel("主机设置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "编辑列表中的已有连接。" if host else
            "新主机可直接写入 ~/.ssh/config；软件不会保存 SSH 密码。"
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self._form = form
        form.setSpacing(13)
        self.alias = QLineEdit(host.alias if host else "")
        self.alias.setPlaceholderText("你为连接起的名称，例如 gpu-server")
        self.write_config = QCheckBox("这是新连接，同时写入 SSH config")
        self.write_config.setChecked(host is None)
        self.write_config.setVisible(host is None)
        self.hostname = QLineEdit()
        self.hostname.setPlaceholderText("服务器 IP 或域名，例如 10.150.16.39")
        self.username = QLineEdit()
        self.username.setPlaceholderText("例如 wfy、root、ubuntu")
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)
        identity_holder = QWidget()
        identity_layout = QHBoxLayout(identity_holder)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        self.identity_file = QLineEdit()
        self.identity_file.setPlaceholderText("可选；留空使用 SSH 默认密钥")
        identity_button = QPushButton("浏览")
        identity_button.clicked.connect(self._choose_identity)
        identity_layout.addWidget(self.identity_file, 1)
        identity_layout.addWidget(identity_button)
        self.proxy_jump = QLineEdit()
        self.proxy_jump.setPlaceholderText("可选；填写已有 SSH 别名，例如 JumpServer")
        self.display_name = QLineEdit(host.display_name if host else "")
        self.display_name.setPlaceholderText("留空则使用 SSH 别名")
        self.remote_port = QSpinBox()
        self.remote_port.setRange(1024, 65535)
        self.remote_port.setValue(host.remote_proxy_port if host else default_port)
        self.remote_dir = QLineEdit(host.remote_dir if host else "~")
        self.auto_reconnect = QCheckBox("连接中断后自动重试")
        self.auto_reconnect.setChecked(host.auto_reconnect if host else True)
        self.auto_start = QCheckBox("软件启动时自动建立隧道")
        self.auto_start.setChecked(host.enabled if host else False)
        form.addRow("SSH 别名", self.alias)
        form.addRow("", self.write_config)
        form.addRow("主机地址", self.hostname)
        form.addRow("用户名", self.username)
        form.addRow("SSH 端口", self.ssh_port)
        form.addRow("私钥文件", identity_holder)
        form.addRow("跳板机", self.proxy_jump)
        form.addRow("显示名称", self.display_name)
        form.addRow("远程代理端口", self.remote_port)
        form.addRow("默认远程目录", self.remote_dir)
        form.addRow("", self.auto_reconnect)
        form.addRow("", self.auto_start)
        layout.addLayout(form)
        self._connection_widgets = [
            self.hostname, self.username, self.ssh_port, identity_holder, self.proxy_jump
        ]
        self.write_config.toggled.connect(self._toggle_connection_fields)
        self._toggle_connection_fields(self.write_config.isChecked() and host is None)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if save_button:
            save_button.setText("保存")
            save_button.setObjectName("primary")
        if cancel_button:
            cancel_button.setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        if not self.alias.text().strip() or any(ch.isspace() for ch in self.alias.text().strip()):
            self.alias.setFocus()
            self.alias.setStyleSheet("border: 1px solid #e05252;")
            return
        if self.write_config.isVisible() and self.write_config.isChecked():
            if not self.hostname.text().strip():
                self.hostname.setFocus()
                return
            if not self.username.text().strip():
                self.username.setFocus()
                return
            identity = self.identity_file.text().strip()
            if identity and not Path(identity).expanduser().is_file():
                QMessageBox.warning(self, "私钥文件不存在", identity)
                self.identity_file.setFocus()
                return
        super().accept()

    def _toggle_connection_fields(self, visible: bool) -> None:
        for widget in self._connection_widgets:
            self._form.setRowVisible(widget, visible)

    def _choose_identity(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 SSH 私钥", str(Path.home() / ".ssh"), "所有文件 (*)"
        )
        if path:
            self.identity_file.setText(path)

    def value(self) -> HostConfig:
        alias = self.alias.text().strip()
        return HostConfig(
            alias=alias, display_name=self.display_name.text().strip() or alias,
            id=self._host.id if self._host else "",
            enabled=self.auto_start.isChecked(), remote_proxy_port=self.remote_port.value(),
            remote_dir=self.remote_dir.text().strip() or "~",
            workspaces=list(host_path for host_path in getattr(self, "_workspaces", [])),
            auto_reconnect=self.auto_reconnect.isChecked(),
            source=self._host.source if self._host else "user_created",
            created_at=self._host.created_at if self._host else utc_now(),
            updated_at=utc_now(),
            extra=dict(self._host.extra) if self._host else {},
        )

    def ssh_entry(self) -> SshHostEntry | None:
        if not self.write_config.isVisible() or not self.write_config.isChecked():
            return None
        return SshHostEntry(
            alias=self.alias.text().strip(),
            hostname=self.hostname.text().strip(),
            user=self.username.text().strip(),
            port=self.ssh_port.value(),
            identity_file=self.identity_file.text().strip(),
            proxy_jump=self.proxy_jump.text().strip(),
        )


class RemoteFolderDialog(QDialog):
    listing_ready = pyqtSignal(int, object)
    listing_failed = pyqtSignal(int, str)

    def __init__(self, host: HostConfig, actions: HostActions, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择 VSCode 工作区")
        self.resize(680, 520)
        self.setMinimumSize(560, 420)
        self.host = host
        self.actions = actions
        self.current_path = host.remote_dir or "~"
        self.parent_path = self.current_path
        self._selected_history_path = ""
        self._request_id = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        title = QLabel("选择远程工作区")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        hint = QLabel(f"通过 SSH 浏览 {host.display_name} 上的文件夹，双击目录可进入。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        history_title = QLabel(f"最近打开的工作区（{len(host.workspaces)}）")
        history_title.setObjectName("sectionTitle")
        layout.addWidget(history_title)
        self.history_list = QListWidget()
        self.history_list.setObjectName("workspaceHistory")
        self.history_list.setMaximumHeight(116)
        self.history_list.setSpacing(1)
        folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirLinkIcon)
        if host.workspaces:
            for path in host.workspaces:
                item = QListWidgetItem(folder_icon, path)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(f"单击选择；双击直接打开\n{path}")
                self.history_list.addItem(item)
        else:
            empty = QListWidgetItem("暂无历史记录，成功打开后会自动保存在这里")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.history_list.addItem(empty)
        self.history_list.itemClicked.connect(self._select_history_item)
        self.history_list.itemDoubleClicked.connect(self._open_history_item)
        layout.addWidget(self.history_list)

        navigation = QHBoxLayout()
        self.up_button = QPushButton("上一级")
        self.refresh_button = QPushButton("刷新")
        self.location = QComboBox()
        self.location.setEditable(True)
        self.location.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        choices: list[str] = []
        for value in [host.remote_dir, *host.workspaces, "~"]:
            value = value.strip()
            if value and value not in choices:
                choices.append(value)
        self.location.addItems(choices)
        self.location.setCurrentText(self.current_path)
        self.location.setPlaceholderText("远程文件夹路径")
        self.go_button = QPushButton("转到")
        navigation.addWidget(self.up_button)
        navigation.addWidget(self.location, 1)
        navigation.addWidget(self.go_button)
        navigation.addWidget(self.refresh_button)
        layout.addLayout(navigation)

        self.directory_list = QListWidget()
        self.directory_list.setAlternatingRowColors(False)
        self.directory_list.setSpacing(2)
        self.directory_list.itemDoubleClicked.connect(self._enter_item)
        self.directory_list.itemSelectionChanged.connect(self._directory_selected)
        layout.addWidget(self.directory_list, 1)
        self.status = QLabel("正在读取远程目录…")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)

        self.remember = QCheckBox("设为这台主机的默认工作区")
        self.remember.setChecked(True)
        layout.addWidget(self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Open)
        open_button = buttons.button(QDialogButtonBox.StandardButton.Open)
        if open_button:
            open_button.setText("选择文件夹并打开")
            open_button.setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.up_button.clicked.connect(lambda: self.load_path(self.parent_path))
        self.refresh_button.clicked.connect(lambda: self.load_path(self.current_path))
        self.go_button.clicked.connect(lambda: self.load_path(self.location.currentText().strip()))
        if self.location.lineEdit():
            self.location.lineEdit().returnPressed.connect(
                lambda: self.load_path(self.location.currentText().strip())
            )
        self.listing_ready.connect(self._show_listing)
        self.listing_failed.connect(self._show_error)
        self.load_path(self.current_path)

    def load_path(self, path: str) -> None:
        path = path.strip() or "~"
        self._selected_history_path = ""
        self.history_list.clearSelection()
        self._request_id += 1
        request_id = self._request_id
        self.status.setText(f"正在读取 {path} …")
        self.directory_list.setEnabled(False)
        threading.Thread(
            target=self._load_worker, args=(request_id, path), daemon=True
        ).start()

    def _load_worker(self, request_id: int, path: str) -> None:
        try:
            listing = self.actions.list_remote_directories(self.host, path)
            self.listing_ready.emit(request_id, listing)
        except Exception as exc:
            self.listing_failed.emit(request_id, str(exc))

    def _show_listing(self, request_id: int, listing: RemoteDirectoryListing) -> None:
        if request_id != self._request_id:
            return
        self.current_path = listing.path
        self.parent_path = listing.parent
        self.location.setCurrentText(listing.path)
        if self.location.findText(listing.path) < 0:
            self.location.insertItem(0, listing.path)
        self.directory_list.clear()
        folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        for name, full_path in listing.directories:
            item = QListWidgetItem(folder_icon, name)
            item.setData(Qt.ItemDataRole.UserRole, full_path)
            item.setToolTip(full_path)
            self.directory_list.addItem(item)
        self.directory_list.setEnabled(True)
        self.status.setText(f"当前文件夹：{listing.path}    ·    {len(listing.directories)} 个子目录")

    def _show_error(self, request_id: int, message: str) -> None:
        if request_id != self._request_id:
            return
        self.directory_list.setEnabled(True)
        self.status.setText("读取失败")
        QMessageBox.warning(self, "无法读取远程文件夹", message)

    def _enter_item(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.load_path(path)

    def _select_history_item(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        self._selected_history_path = path
        self.directory_list.clearSelection()
        self.location.setCurrentText(path)
        self.status.setText(f"已选择历史工作区：{path}")

    def _open_history_item(self, item: QListWidgetItem) -> None:
        self._select_history_item(item)
        if self._selected_history_path:
            super().accept()

    def _directory_selected(self) -> None:
        if not self.directory_list.selectedItems():
            return
        self._selected_history_path = ""
        self.history_list.clearSelection()

    def accept(self) -> None:
        if not self.path():
            self.location.setFocus()
            return
        super().accept()

    def path(self) -> str:
        if self._selected_history_path:
            return self._selected_history_path
        selected_items = self.directory_list.selectedItems()
        if selected_items:
            selected = selected_items[0]
            return selected.data(Qt.ItemDataRole.UserRole) or self.current_path
        return self.current_path


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("全局设置")
        self.setMinimumWidth(590)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        title = QLabel("全局设置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        form.setSpacing(12)
        ssh_holder, self.ssh_path = self._file_row(settings.ssh_path, "选择 ssh.exe")
        config_holder, self.config_path = self._file_row(settings.ssh_config_path, "选择 SSH config")
        vscode_holder, self.vscode_path = self._file_row(settings.vscode_path, "选择 Code.exe 或 code.cmd")
        self.local_host = QLineEdit(settings.local_proxy_host)
        self.local_port = self._spin(settings.local_proxy_port, 1, 65535)
        self.default_remote_port = self._spin(settings.default_remote_proxy_port, 1024, 65535)
        self.keepalive = self._spin(settings.keepalive_interval, 5, 300)
        self.keepalive_count = self._spin(settings.keepalive_count_max, 1, 10)
        self.smoke_timeout = self._spin(settings.smoke_timeout, 10, 180)
        self.health_interval = self._spin(settings.health_probe_interval, 15, 3600)
        self.automatic_health = QCheckBox("定时检测当前主机链路")
        self.automatic_health.setChecked(settings.automatic_health_checks)
        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level.setCurrentText(settings.log_level.upper())
        self.log_retention = self._spin(settings.log_retention_days, 1, 365)
        self.codex_log_level = QComboBox()
        self.codex_log_level.addItems(["warn", "info", "debug", "trace"])
        self.codex_log_level.setCurrentText(settings.codex_log_level.lower())
        self.ssh_debug = QCheckBox("诊断模式记录 SSH -vv 输出")
        self.ssh_debug.setChecked(settings.ssh_debug_logging)
        self.clash_controller = QLineEdit(settings.clash_controller_url)
        self.clash_controller.setPlaceholderText("可选，例如 http://127.0.0.1:9090")
        self.clash_secret = QLineEdit(settings.clash_controller_secret)
        self.clash_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.clash_secret.setPlaceholderText("可选；日志和诊断包不会记录此值")
        self.check_updates = QCheckBox("启动时检查 GitHub Release 更新")
        self.check_updates.setChecked(settings.check_updates_on_launch)
        self.minimize_tray = QCheckBox("关闭窗口时最小化到系统托盘")
        self.minimize_tray.setChecked(settings.minimize_to_tray)
        form.addRow("SSH 程序", ssh_holder)
        form.addRow("SSH 配置", config_holder)
        form.addRow("VSCode 程序", vscode_holder)
        form.addRow("本地代理地址", self.local_host)
        form.addRow("本地代理端口", self.local_port)
        form.addRow("新主机默认远程端口", self.default_remote_port)
        form.addRow("保活间隔（秒）", self.keepalive)
        form.addRow("失联判定次数", self.keepalive_count)
        form.addRow("Codex 测试上限（秒）", self.smoke_timeout)
        form.addRow("链路检测间隔（秒）", self.health_interval)
        form.addRow("", self.automatic_health)
        form.addRow("应用日志级别", self.log_level)
        form.addRow("日志保留天数", self.log_retention)
        form.addRow("Codex 日志级别", self.codex_log_level)
        form.addRow("Clash Controller", self.clash_controller)
        form.addRow("Clash Secret", self.clash_secret)
        form.addRow("", self.ssh_debug)
        form.addRow("", self.check_updates)
        form.addRow("", self.minimize_tray)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _file_row(self, value: str, caption: str) -> tuple[QWidget, QLineEdit]:
        widget = QWidget()
        holder = QHBoxLayout(widget)
        holder.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(value)
        button = QPushButton("浏览")
        holder.addWidget(edit, 1)
        holder.addWidget(button)
        button.clicked.connect(lambda: self._choose_file(edit, caption))
        return widget, edit

    def _choose_file(self, edit: QLineEdit, caption: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, caption, edit.text())
        if path:
            edit.setText(path)

    @staticmethod
    def _spin(value: int, minimum: int, maximum: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    def apply_to(self, settings: AppSettings) -> None:
        settings.ssh_path = self.ssh_path.text().strip()
        settings.ssh_config_path = self.config_path.text().strip()
        settings.vscode_path = self.vscode_path.text().strip()
        settings.local_proxy_host = self.local_host.text().strip()
        settings.local_proxy_port = self.local_port.value()
        settings.default_remote_proxy_port = self.default_remote_port.value()
        settings.keepalive_interval = self.keepalive.value()
        settings.keepalive_count_max = self.keepalive_count.value()
        settings.smoke_timeout = self.smoke_timeout.value()
        settings.health_probe_interval = self.health_interval.value()
        settings.automatic_health_checks = self.automatic_health.isChecked()
        settings.log_level = self.log_level.currentText()
        settings.log_retention_days = self.log_retention.value()
        settings.codex_log_level = self.codex_log_level.currentText()
        settings.ssh_debug_logging = self.ssh_debug.isChecked()
        settings.clash_controller_url = self.clash_controller.text().strip()
        settings.clash_controller_secret = self.clash_secret.text()
        settings.check_updates_on_launch = self.check_updates.isChecked()
        settings.minimize_to_tray = self.minimize_tray.isChecked()


class TextViewerDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        search = QLineEdit()
        search.setPlaceholderText("在当前日志中搜索")
        layout.addWidget(search)
        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        self.viewer.setPlainText(text)
        layout.addWidget(self.viewer, 1)
        search.returnPressed.connect(lambda: self.viewer.find(search.text()))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class StructuredLogDialog(QDialog):
    def __init__(self, log_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.log_dir = log_dir
        self.records: list[dict] = []
        self.setWindowTitle("持久日志")
        self.resize(1050, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        heading = QLabel(f"持久日志 · {log_dir}")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        filters = QHBoxLayout()
        self.level = QComboBox()
        self.component = QComboBox()
        self.host = QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索事件、消息和详细信息")
        for combo, label in [
            (self.level, "全部级别"), (self.component, "全部组件"), (self.host, "全部主机")
        ]:
            combo.addItem(label, "")
        filters.addWidget(self.level)
        filters.addWidget(self.component)
        filters.addWidget(self.host)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        self.viewer = QPlainTextEdit()
        self.viewer.setReadOnly(True)
        layout.addWidget(self.viewer, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        refresh = buttons.addButton("刷新", QDialogButtonBox.ButtonRole.ActionRole)
        refresh.clicked.connect(self.reload)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.level.currentIndexChanged.connect(self.apply_filters)
        self.component.currentIndexChanged.connect(self.apply_filters)
        self.host.currentIndexChanged.connect(self.apply_filters)
        self.search.textChanged.connect(self.apply_filters)
        self.reload()

    def reload(self) -> None:
        self.records.clear()
        paths = sorted(self.log_dir.glob("events.jsonl*"), key=lambda path: path.stat().st_mtime)
        for path in paths:
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        self.records.append(value)
            except OSError:
                continue
        self.records = self.records[-10_000:]
        levels = sorted({str(item.get("level", "")) for item in self.records if item.get("level")})
        components = sorted({str(item.get("logger", "")) for item in self.records if item.get("logger")})
        hosts = sorted({str(item.get("host", "")) for item in self.records if item.get("host")})
        self._replace_choices(self.level, "全部级别", levels)
        self._replace_choices(self.component, "全部组件", components)
        self._replace_choices(self.host, "全部主机", hosts)
        self.apply_filters()

    @staticmethod
    def _replace_choices(combo: QComboBox, all_label: str, values: list[str]) -> None:
        selected = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_label, "")
        for value in values:
            combo.addItem(value, value)
        index = combo.findData(selected)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def apply_filters(self) -> None:
        level = str(self.level.currentData() or "")
        component = str(self.component.currentData() or "")
        host = str(self.host.currentData() or "")
        needle = self.search.text().strip().casefold()
        lines: list[str] = []
        for item in self.records:
            if level and item.get("level") != level:
                continue
            if component and item.get("logger") != component:
                continue
            if host and item.get("host") != host:
                continue
            raw = json.dumps(item, ensure_ascii=False)
            if needle and needle not in raw.casefold():
                continue
            event = item.get("event") or item.get("message") or "event"
            prefix = f"{item.get('time', '')} {str(item.get('level', '')).upper():7} {event}"
            details = {
                key: value for key, value in item.items()
                if key not in {"time", "level", "event", "message", "session_id"}
            }
            lines.append(prefix + ("  " + json.dumps(details, ensure_ascii=False) if details else ""))
        self.viewer.setPlainText("\n".join(lines) if lines else "没有符合筛选条件的日志。")
