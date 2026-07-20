from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QStyle, QVBoxLayout, QWidget,
)

from .actions import HostActions, RemoteDirectoryListing
from .models import AppSettings, HostConfig


class HostDialog(QDialog):
    def __init__(self, parent=None, host: HostConfig | None = None, default_port: int = 10099) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑主机" if host else "添加主机")
        self.setMinimumWidth(440)
        self._workspaces = list(host.workspaces) if host else []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        title = QLabel("主机设置")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        hint = QLabel("SSH 别名来自 ~/.ssh/config；软件不会保存密码或私钥。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        form.setSpacing(13)
        self.alias = QLineEdit(host.alias if host else "")
        self.alias.setPlaceholderText("例如 10.150.16.39 或 myserver")
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
        form.addRow("显示名称", self.display_name)
        form.addRow("远程代理端口", self.remote_port)
        form.addRow("默认远程目录", self.remote_dir)
        form.addRow("", self.auto_reconnect)
        form.addRow("", self.auto_start)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        if not self.alias.text().strip() or any(ch.isspace() for ch in self.alias.text().strip()):
            self.alias.setFocus()
            self.alias.setStyleSheet("border: 1px solid #e05252;")
            return
        super().accept()

    def value(self) -> HostConfig:
        alias = self.alias.text().strip()
        return HostConfig(
            alias=alias, display_name=self.display_name.text().strip() or alias,
            enabled=self.auto_start.isChecked(), remote_proxy_port=self.remote_port.value(),
            remote_dir=self.remote_dir.text().strip() or "~",
            workspaces=list(host_path for host_path in getattr(self, "_workspaces", [])),
            auto_reconnect=self.auto_reconnect.isChecked(),
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

    def accept(self) -> None:
        if not self.path():
            self.location.setFocus()
            return
        super().accept()

    def path(self) -> str:
        selected = self.directory_list.currentItem()
        if selected:
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
        settings.minimize_to_tray = self.minimize_tray.isChecked()
