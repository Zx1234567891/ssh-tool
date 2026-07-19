from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .models import AppSettings, HostConfig


class HostDialog(QDialog):
    def __init__(self, parent=None, host: HostConfig | None = None, default_port: int = 10099) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑主机" if host else "添加主机")
        self.setMinimumWidth(440)
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
            auto_reconnect=self.auto_reconnect.isChecked(),
        )


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
        settings.local_proxy_host = self.local_host.text().strip()
        settings.local_proxy_port = self.local_port.value()
        settings.default_remote_proxy_port = self.default_remote_port.value()
        settings.keepalive_interval = self.keepalive.value()
        settings.keepalive_count_max = self.keepalive_count.value()
        settings.smoke_timeout = self.smoke_timeout.value()
        settings.minimize_to_tray = self.minimize_tray.isChecked()
