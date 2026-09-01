import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import vpngate_manager as manager


class CandidateExitIPTests(unittest.TestCase):
    def test_interface_exit_check_falls_back_and_accepts_only_a_valid_ip(self):
        responses = [
            subprocess.CompletedProcess([], 1, stdout="", stderr="first failed"),
            subprocess.CompletedProcess([], 0, stdout="203.0.113.42\n", stderr=""),
        ]
        checker = getattr(manager, "check_interface_exit_ip", lambda *_args, **_kwargs: (False, ""))

        with mock.patch.object(manager.subprocess, "run", side_effect=responses) as run:
            actual = checker("tun7", timeout=3)

        self.assertEqual(actual, (True, "203.0.113.42"))
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[0], "curl")
            self.assertEqual(command[command.index("--interface") + 1], "tun7")
            self.assertEqual(command[command.index("--max-time") + 1], "3")

    def test_interface_exit_check_rejects_invalid_and_failed_responses(self):
        responses = [
            subprocess.CompletedProcess([], 0, stdout="not-an-ip\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="<html>blocked</html>\n", stderr=""),
        ]
        checker = getattr(manager, "check_interface_exit_ip", lambda *_args, **_kwargs: (True, "invalid"))

        with mock.patch.object(manager.subprocess, "run", side_effect=responses):
            actual = checker("tun8")

        self.assertEqual(actual, (False, ""))

    def test_probe_keeps_tunnel_until_exit_ip_is_recorded_then_reaps_it(self):
        events = []
        process = SimpleNamespace(poll=lambda: None)
        node = {
            "id": "jp-one",
            "config_text": "remote 198.51.100.10 443 tcp\n",
            "remote_host": "198.51.100.10",
            "remote_port": 443,
            "ping": 18,
        }

        def start(_config, keep_alive, route_nopull, timeout, dev):
            events.append(("openvpn", keep_alive, route_nopull, timeout, dev))
            return True, "connected", process

        def check(interface, timeout=6):
            self.assertIsNone(process.poll())
            events.append(("exit", interface, timeout))
            return True, "203.0.113.9"

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(manager, "CONFIG_DIR", Path(directory)),
                mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=18),
                mock.patch.object(manager, "get_free_test_index", return_value=7),
                mock.patch.object(manager, "release_test_index", side_effect=lambda index: events.append(("release", index))),
                mock.patch.object(manager, "run_openvpn_until_ready", side_effect=start),
                mock.patch.object(manager, "setup_policy_routing", side_effect=lambda interface, table: events.append(("route", interface, table)) or True),
                mock.patch.object(manager, "cleanup_policy_routing", side_effect=lambda table: events.append(("cleanup", table))),
                mock.patch.object(manager, "check_interface_exit_ip", create=True, side_effect=check),
                mock.patch.object(manager, "stop_process", side_effect=lambda value: events.append(("stop", value))),
                mock.patch.object(manager.time, "time", return_value=1_700_000_123.5),
            ):
                actual = manager._probe_one_node(node)

        self.assertEqual(actual["probe_status"], "available")
        self.assertEqual(actual.get("exit_ip"), "203.0.113.9")
        self.assertEqual(actual.get("exit_ip_checked_at"), 1_700_000_123.5)
        self.assertEqual(
            events,
            [
                ("openvpn", True, True, 12, "tun7"),
                ("route", "tun7", 61_007),
                ("exit", "tun7", 6),
                ("cleanup", 61_007),
                ("stop", process),
                ("release", 7),
            ],
        )

    def test_probe_marks_candidate_unavailable_and_reaps_tunnel_when_exit_check_raises(self):
        events = []
        process = SimpleNamespace(poll=lambda: None)
        node = {
            "id": "jp-broken",
            "config_text": "remote 198.51.100.11 443 tcp\n",
            "remote_host": "198.51.100.11",
            "remote_port": 443,
            "ping": 20,
        }

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(manager, "CONFIG_DIR", Path(directory)),
                mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=20),
                mock.patch.object(manager, "get_free_test_index", return_value=8),
                mock.patch.object(manager, "release_test_index", side_effect=lambda index: events.append(("release", index))),
                mock.patch.object(manager, "run_openvpn_until_ready", return_value=(True, "connected", process)),
                mock.patch.object(manager, "setup_policy_routing", return_value=True),
                mock.patch.object(manager, "cleanup_policy_routing", side_effect=lambda table: events.append(("cleanup", table))),
                mock.patch.object(manager, "check_interface_exit_ip", create=True, side_effect=RuntimeError("probe failed")),
                mock.patch.object(manager, "stop_process", side_effect=lambda value: events.append(("stop", value))),
            ):
                actual = manager._probe_one_node(node)

        self.assertEqual(actual["probe_status"], "unavailable")
        self.assertEqual(actual["probe_message"], "公网出口检测失败")
        self.assertNotIn("exit_ip", actual)
        self.assertEqual(events, [("cleanup", 61_008), ("stop", process), ("release", 8)])

    def test_probe_does_not_check_public_ip_when_temporary_routing_fails(self):
        events = []
        process = SimpleNamespace(poll=lambda: None)
        node = {
            "id": "jp-no-route",
            "config_text": "remote 198.51.100.12 443 tcp\n",
            "remote_host": "198.51.100.12",
            "remote_port": 443,
            "ping": 21,
        }

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.object(manager, "CONFIG_DIR", Path(directory)),
                mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=21),
                mock.patch.object(manager, "get_free_test_index", return_value=9),
                mock.patch.object(manager, "release_test_index", side_effect=lambda index: events.append(("release", index))),
                mock.patch.object(manager, "run_openvpn_until_ready", return_value=(True, "connected", process)),
                mock.patch.object(manager, "setup_policy_routing", side_effect=lambda interface, table: events.append(("route", interface, table)) or False),
                mock.patch.object(manager, "cleanup_policy_routing", side_effect=lambda table: events.append(("cleanup", table))),
                mock.patch.object(manager, "check_interface_exit_ip", create=True) as check,
                mock.patch.object(manager, "stop_process", side_effect=lambda value: events.append(("stop", value))),
            ):
                actual = manager._probe_one_node(node)

        self.assertEqual(actual["probe_status"], "unavailable")
        self.assertEqual(actual["probe_message"], "临时出口路由配置失败")
        check.assert_not_called()
        self.assertEqual(
            events,
            [
                ("route", "tun9", 61_009),
                ("cleanup", 61_009),
                ("stop", process),
                ("release", 9),
            ],
        )


if __name__ == "__main__":
    unittest.main()
