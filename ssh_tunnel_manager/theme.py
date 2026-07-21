APP_STYLE = r"""
* { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #1f2329; }
QMainWindow, QDialog { background: #f5f6f7; }
QWidget#sidebar { background: #eef0f2; border-right: 1px solid #e2e5e8; }
QWidget#content { background: #f7f8fa; }
QLabel#brand { font-size: 19px; font-weight: 700; color: #171a1d; }
QLabel#pageTitle { font-size: 24px; font-weight: 700; color: #171a1d; }
QLabel#muted { color: #89909a; }
QLabel#sectionTitle { font-size: 15px; font-weight: 650; }
QFrame#card { background: white; border: 1px solid #e8eaed; border-radius: 12px; }
QLineEdit, QSpinBox, QComboBox { background: white; border: 1px solid #dfe3e8; border-radius: 7px; padding: 7px 10px; min-height: 20px; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #07c160; }
QPushButton { background: white; border: 1px solid #dfe3e8; border-radius: 7px; padding: 8px 14px; }
QPushButton:hover { background: #f3f5f6; border-color: #cbd0d6; }
QPushButton:pressed { background: #e9ecef; }
QPushButton#primary { color: white; background: #07c160; border: 1px solid #07c160; font-weight: 600; }
QPushButton#primary:hover { background: #06ad56; }
QPushButton#danger { color: #d64545; background: #fff; border-color: #f0caca; }
QPushButton#ghost { background: transparent; border: none; padding: 6px; color: #6d747d; }
QPushButton#settingsButton { background: #ffffff; border: 1px solid #d9dde3; border-radius: 8px; padding: 7px 11px; color: #454b54; font-weight: 600; }
QPushButton#settingsButton:hover { background: #f7f9fa; border-color: #aeb5bf; color: #171a1d; }
QPushButton#settingsButton:pressed { background: #edf0f2; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { border: none; margin: 2px 8px; border-radius: 8px; }
QListWidget::item:selected { background: #dceee5; }
QListWidget::item:hover:!selected { background: #e7e9eb; }
QTreeWidget { background: transparent; border: none; outline: none; }
QTreeWidget::item { border: none; padding: 3px 5px; margin: 1px 2px; border-radius: 7px; }
QTreeWidget::item:selected { background: #dceee5; }
QTreeWidget::item:hover:!selected { background: #e7e9eb; }
QTreeWidget::branch { background: transparent; }
QListWidget#workspaceHistory { background: #f7f8fa; border: 1px solid #e2e5e8; border-radius: 8px; padding: 3px; }
QListWidget#workspaceHistory::item { margin: 1px; padding: 5px 7px; }
QPlainTextEdit { background: #20242a; color: #d7dce2; border: none; border-radius: 9px; padding: 9px; font-family: Consolas; font-size: 12px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QComboBox::drop-down { border: none; width: 24px; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #c7ccd2; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

STATE_COLORS = {
    "stopped": "#a7adb5",
    "connecting": "#e6a23c",
    "connected": "#07c160",
    "retrying": "#e6a23c",
    "error": "#e05252",
}

STATE_BACKGROUNDS = {
    "stopped": "#eef0f2",
    "connecting": "#fff5e6",
    "connected": "#e8f8ef",
    "retrying": "#fff5e6",
    "error": "#fdeeee",
}

STATE_TEXT = {
    "stopped": "未启动",
    "connecting": "连接中",
    "connected": "已连接",
    "retrying": "重连中",
    "error": "异常",
}
