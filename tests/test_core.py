from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ssh_tunnel_manager.actions import HostActions
from ssh_tunnel_manager.models import (
    AppState, HostConfig, connected_hosts_first, find_vscode_path,
)
from ssh_tunnel_manager.ssh_config import (
    SshHostEntry, append_host_entry, parse_host_aliases, upsert_proxy_setenv,
)
from ssh_tunnel_manager.store import StateStore
from ssh_tunnel_manager.tunnel import TunnelManager, TunnelState


class ModelTests(unittest.TestCase):
    def test_connected_hosts_are_stably_ordered_first(self) -> None:
        hosts = [
            HostConfig(alias="stopped-a"), HostConfig(alias="connected-a"),
            HostConfig(alias="stopped-b"), HostConfig(alias="connected-b"),
        ]
        ordered = connected_hosts_first(hosts, {"connected-a", "connected-b"})
        self.assertEqual(
            [host.alias for host in ordered],
            ["connected-a", "connected-b", "stopped-a", "stopped-b"],
        )

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

    def test_proxy_ports_are_upserted_in_one_local_include(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config"
            path.write_text("Host server-a\n  HostName 10.0.0.1\n", encoding="utf-8")
            include_path = upsert_proxy_setenv(str(path), "server-a", 10099)
            upsert_proxy_setenv(str(path), "server-b", 10098)
            upsert_proxy_setenv(str(path), "server-a", 10097)

            main = path.read_text(encoding="utf-8")
            generated = include_path.read_text(encoding="utf-8")
            self.assertEqual(main.count("Include "), 1)
            self.assertEqual(generated.count("Host server-a"), 1)
            self.assertIn("SetEnv LC_STM_PROXY_PORT=10097", generated)
            self.assertIn("SetEnv LC_STM_PROXY_PORT=10098", generated)
            self.assertNotIn("LC_STM_PROXY_PORT=10099", generated)
            self.assertTrue(generated.rstrip().endswith("Host *"))


class TunnelCommandTests(unittest.TestCase):
    def test_dedicated_forward_contains_required_options(self) -> None:
        state = AppState(hosts=[HostConfig(alias="server", remote_proxy_port=11099)])
        manager = TunnelManager(lambda: state.settings, lambda *args: None)
        command = manager._command(state.hosts[0])
        self.assertNotIn("ClearAllForwardings=yes", command)
        self.assertIn("11099:127.0.0.1:7892", command)
        self.assertIn("ExitOnForwardFailure=yes", command)

    def test_discovers_tunnel_owned_by_another_instance(self) -> None:
        state = AppState(hosts=[HostConfig(alias="server", remote_proxy_port=11099)])
        manager = TunnelManager(lambda: state.settings, lambda *args: None)
        rows = [{
            "ProcessId": 4321,
            "CommandLine": (
                "ssh.exe -F config -NT -R 11099:127.0.0.1:7892 server"
            ),
        }]
        with patch.object(manager, "_existing_ssh_processes", return_value=rows):
            discovered = manager.discover_existing(state.hosts)
        runtime = manager.runtime("server")
        self.assertEqual(discovered, ["server"])
        self.assertEqual(runtime.state, TunnelState.CONNECTED)
        self.assertEqual(runtime.external_pid, 4321)
        with (
            patch.object(manager, "_pid_is_running", return_value=True),
            patch("ssh_tunnel_manager.tunnel.subprocess.Popen") as popen,
        ):
            manager.start(state.hosts[0])
        popen.assert_not_called()


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

    def test_vscode_launch_reuses_default_profile_and_syncs_ssh_port(self) -> None:
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
                patch.object(actions, "configure_local_ssh_proxy") as configure,
                patch("ssh_tunnel_manager.actions.subprocess.Popen") as popen,
            ):
                actions.launch_vscode(host)
            command = popen.call_args.args[0]
            self.assertEqual(command[0], str(fake_code))
            self.assertNotIn("--user-data-dir", command)
            self.assertIn("--new-window", command)
            self.assertEqual(command[-3:], ["--remote", "ssh-remote+server-a", "/workspace"])
            configure.assert_called_once_with(host)

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
        self.assertIn("LC_STM_PROXY_PORT", script)
        self.assertIn('stm_proxy_use "$LC_STM_PROXY_PORT"', script)
        self.assertNotIn("10099", script)
        self.assertIn("localhost|127[.]0[.]0[.]1", script)
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
