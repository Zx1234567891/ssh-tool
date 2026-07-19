from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ssh_tunnel_manager.models import AppState, HostConfig
from ssh_tunnel_manager.ssh_config import parse_host_aliases
from ssh_tunnel_manager.store import StateStore
from ssh_tunnel_manager.tunnel import TunnelManager


class ModelTests(unittest.TestCase):
    def test_state_round_trip(self) -> None:
        original = AppState(hosts=[HostConfig(alias="server-a", display_name="实验机")])
        restored = AppState.from_dict(original.to_dict())
        self.assertEqual(restored.hosts[0].alias, "server-a")
        self.assertEqual(restored.hosts[0].display_name, "实验机")

    def test_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(Path(folder))
            store.save(AppState(hosts=[HostConfig(alias="server-b")]))
            self.assertEqual(store.load().hosts[0].alias, "server-b")


class SshConfigTests(unittest.TestCase):
    def test_parses_aliases_and_ignores_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config"
            path.write_text(
                "Host server-a 10.0.0.2\n  User demo\nHost *\nHost !blocked\nHost server-a\n",
                encoding="utf-8",
            )
            self.assertEqual(parse_host_aliases(str(path)), ["server-a", "10.0.0.2"])

    def test_include(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "extra.conf").write_text("Host jump\n", encoding="utf-8")
            (root / "config").write_text("Include extra.conf\nHost target\n", encoding="utf-8")
            self.assertEqual(parse_host_aliases(str(root / "config")), ["jump", "target"])


class TunnelCommandTests(unittest.TestCase):
    def test_dedicated_forward_contains_required_options(self) -> None:
        state = AppState(hosts=[HostConfig(alias="server", remote_proxy_port=11099)])
        manager = TunnelManager(lambda: state.settings, lambda *args: None)
        command = manager._command(state.hosts[0])
        self.assertNotIn("ClearAllForwardings=yes", command)
        self.assertIn("11099:127.0.0.1:7892", command)
        self.assertIn("ExitOnForwardFailure=yes", command)


if __name__ == "__main__":
    unittest.main()
