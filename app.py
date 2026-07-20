from __future__ import annotations

import argparse
from pathlib import Path
import sys


def self_test() -> int:
    from ssh_tunnel_manager.models import AppState, HostConfig
    from ssh_tunnel_manager.ssh_config import parse_host_aliases
    from ssh_tunnel_manager.tunnel import TunnelManager

    state = AppState(hosts=[HostConfig(alias="example")])
    assert state.to_dict()["hosts"][0]["alias"] == "example"
    assert parse_host_aliases(str(Path(__file__).with_name("missing-config"))) == []
    manager = TunnelManager(lambda: state.settings, lambda *args: None)
    command = manager._command(state.hosts[0])
    assert "ClearAllForwardings=yes" not in command
    assert "10099:127.0.0.1:7892" in command
    print("SELF_TEST_OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SSH 隧道助手")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--start-enabled", action="store_true",
        help="start all hosts marked as enabled for this launch",
    )
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication
    from ssh_tunnel_manager.resources import resource_path
    from ssh_tunnel_manager.theme import APP_STYLE
    from ssh_tunnel_manager.ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("SSH 隧道助手")
    app.setOrganizationName("Elpco")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    app.setWindowIcon(QIcon(str(resource_path("assets/logo.png"))))
    window = MainWindow(start_enabled_now=args.start_enabled)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
