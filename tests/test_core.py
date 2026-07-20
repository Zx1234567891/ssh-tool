from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ssh_tunnel_manager.actions import HostActions
from ssh_tunnel_manager.models import AppState, HostConfig, find_vscode_path
from ssh_tunnel_manager.ssh_config import SshHostEntry, append_host_entry, parse_host_aliases
from ssh_tunnel_manager.store import StateStore
from ssh_tunnel_manager.tunnel import TunnelManager


class ModelTests(unittest.TestCase):
    def test_state_round_trip(self) -> None:
        original = AppState(hosts=[HostConfig(alias="server-a", display_name="实验机", workspaces=["/workspace/a"])])
        restored = AppState.from_dict(original.to_dict())
        self.assertEqual(restored.hosts[0].alias, "server-a")
        self.assertEqual(restored.hosts[0].display_name, "实验机")
        self.assertEqual(restored.hosts[0].workspaces, ["/workspace/a"])

    def test_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = StateStore(Path(folder))
            store.save(AppState(hosts=[HostConfig(alias="server-b")]))
            self.assertEqual(store.load().hosts[0].alias, "server-b")

    def test_vscode_path_is_detected_on_developer_machine(self) -> None:
        detected = find_vscode_path()
        if detected:
            self.assertTrue(Path(detected).is_file())


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

    def test_append_new_host_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config"
            path.write_text("Host existing\n  HostName 10.0.0.1\n", encoding="utf-8")
            backup = append_host_entry(
                str(path),
                SshHostEntry(
                    alias="gpu-server", hostname="10.0.0.2", user="demo",
                    port=2222, identity_file=str(Path(folder) / "id_ed25519"),
                    proxy_jump="jump",
                ),
            )
            self.assertIsNotNone(backup)
            self.assertTrue(backup.is_file())
            text = path.read_text(encoding="utf-8")
            self.assertIn("Host gpu-server", text)
            self.assertIn("HostName 10.0.0.2", text)
            self.assertIn("Port 2222", text)
            self.assertIn("ProxyJump jump", text)

    def test_append_rejects_duplicate_alias(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config"
            path.write_text("Host existing\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                append_host_entry(
                    str(path), SshHostEntry(alias="existing", hostname="10.0.0.2", user="demo")
                )


class TunnelCommandTests(unittest.TestCase):
    def test_dedicated_forward_contains_required_options(self) -> None:
        state = AppState(hosts=[HostConfig(alias="server", remote_proxy_port=11099)])
        manager = TunnelManager(lambda: state.settings, lambda *args: None)
        command = manager._command(state.hosts[0])
        self.assertNotIn("ClearAllForwardings=yes", command)
        self.assertIn("11099:127.0.0.1:7892", command)
        self.assertIn("ExitOnForwardFailure=yes", command)


class ActionTests(unittest.TestCase):
    def test_local_proxy_requires_real_http_connect(self) -> None:
        state = AppState()
        actions = HostActions(lambda: state.settings)
        failed = subprocess.CompletedProcess(
            args=[], returncode=56, stdout="", stderr="Proxy CONNECT aborted"
        )
        with patch.object(actions, "_run", return_value=failed):
            result = actions.test_local_proxy()
        self.assertFalse(result.ok)
        self.assertIn("Proxy CONNECT aborted", result.detail)

    def test_local_proxy_accepts_connect_before_windows_tls_error(self) -> None:
        state = AppState()
        actions = HostActions(lambda: state.settings)
        connected = subprocess.CompletedProcess(
            args=[], returncode=35,
            stdout="HTTP/1.1 200 Connection established\n",
            stderr="schannel: SEC_E_NO_CREDENTIALS",
        )
        with patch.object(actions, "_run", return_value=connected):
            result = actions.test_local_proxy()
        self.assertTrue(result.ok)
        self.assertIn("200 Connection established", result.detail)

    def test_vscode_launch_uses_per_host_proxy_profile(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            fake_code = Path(folder) / "Code.exe"
            fake_code.write_bytes(b"not-executed")
            state = AppState()
            state.settings.vscode_path = str(fake_code)
            actions = HostActions(lambda: state.settings)
            host = HostConfig(
                alias="server-a", remote_dir="/workspace", remote_proxy_port=11099
            )
            with (
                patch.dict(os.environ, {"LOCALAPPDATA": folder}),
                patch("ssh_tunnel_manager.actions.subprocess.Popen") as popen,
            ):
                actions.launch_vscode(host)
            command = popen.call_args.args[0]
            self.assertEqual(command[0], str(fake_code))
            self.assertIn("--user-data-dir", command)
            self.assertIn("--new-window", command)
            self.assertEqual(command[-3:], ["--remote", "ssh-remote+server-a", "/workspace"])
            profile = Path(command[command.index("--user-data-dir") + 1])
            settings = json.loads((profile / "User" / "settings.json").read_text(encoding="utf-8"))
            environment = settings["terminal.integrated.env.linux"]
            self.assertEqual(environment["http_proxy"], "http://127.0.0.1:11099")
            self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:11099")

    def test_vscode_profiles_are_local_and_host_specific(self) -> None:
        state = AppState()
        actions = HostActions(lambda: state.settings)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            host = HostConfig(alias="shared-user-host", remote_proxy_port=10099)
            with patch.dict(os.environ, {"LOCALAPPDATA": first}):
                first_profile = actions.configure_vscode_profile(host)
            host.remote_proxy_port = 10098
            with patch.dict(os.environ, {"LOCALAPPDATA": second}):
                second_profile = actions.configure_vscode_profile(host)
            first_settings = json.loads(
                (first_profile / "User" / "settings.json").read_text(encoding="utf-8")
            )
            second_settings = json.loads(
                (second_profile / "User" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                first_settings["terminal.integrated.env.linux"]["http_proxy"],
                "http://127.0.0.1:10099",
            )
            self.assertEqual(
                second_settings["terminal.integrated.env.linux"]["http_proxy"],
                "http://127.0.0.1:10098",
            )

    def test_remote_shell_installs_selector_without_fixed_port(self) -> None:
        state = AppState()
        actions = HostActions(lambda: state.settings)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="CONFIG_OK backup=test", stderr=""
        )
        with patch.object(actions, "_run", return_value=completed) as run:
            result = actions.configure_remote_shell(
                HostConfig(alias="server-a", remote_proxy_port=10099)
            )
        script = run.call_args.args[2]
        command = run.call_args.args[0]
        self.assertTrue(result.ok)
        self.assertIn("stm_proxy_use", script)
        self.assertIn("stm_proxy_off", script)
        self.assertNotIn("10099", script)
        self.assertEqual(command[-1], "tr -d '\\015' | bash -s")

    def test_terminal_and_codex_receive_selected_proxy_port(self) -> None:
        state = AppState()
        actions = HostActions(lambda: state.settings)
        host = HostConfig(alias="server-a", remote_proxy_port=10098)
        with patch("ssh_tunnel_manager.actions.subprocess.Popen") as popen:
            actions.launch_terminal(host)
            terminal_command = popen.call_args.args[0]
            actions.launch_codex(host)
            codex_command = popen.call_args.args[0]
        self.assertIn("http_proxy=http://127.0.0.1:10098", terminal_command[-1])
        self.assertIn("http_proxy=http://127.0.0.1:10098", codex_command[-1])

    def test_remote_directory_listing_is_parsed(self) -> None:
        state = AppState()
        actions = HostActions(lambda: state.settings)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"path":"/home/demo","parent":"/home","directories":[["project","/home/demo/project"]]}\n',
            stderr="",
        )
        with patch.object(actions, "_run", return_value=completed):
            listing = actions.list_remote_directories(HostConfig(alias="server-a"), "~")
        self.assertEqual(listing.path, "/home/demo")
        self.assertEqual(listing.directories, [("project", "/home/demo/project")])


if __name__ == "__main__":
    unittest.main()
