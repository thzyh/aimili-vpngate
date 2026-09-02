import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vpngate_manager as manager


class ExitSlotTypeTests(unittest.TestCase):
    def test_no_route_to_host_is_a_persistable_candidate_dial_failure(self):
        code, message = manager.vpn_utils.diagnose_openvpn_failure([
            "TCP: connect to remote failed: No route to host",
            "Exiting due to fatal error",
        ])

        self.assertEqual(code, 2004)
        self.assertIn("ERR_OVPN_NO_ROUTE_TO_HOST", message)
        self.assertEqual(
            manager._candidate_dial_failure_code(message),
            "candidate_dial_failed",
        )

    def test_mark_candidate_unavailable_persists_blacklist_and_pool_state(self):
        candidate = {
            "id": "jp-stale",
            "country_short": "JP",
            "country": "Japan",
            "ip": "198.51.100.30",
            "exit_ip": "203.0.113.30",
            "exit_ip_checked_at": 90.0,
            "ip_type": "hosting",
            "probe_status": "available",
            "config_text": "client",
        }
        with tempfile.TemporaryDirectory() as directory:
            nodes_file = Path(directory) / "nodes.json"
            blacklist_file = Path(directory) / "blacklist.json"
            nodes_file.write_text(json.dumps([candidate]), encoding="utf-8")
            blacklist_file.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(manager, "NODES_FILE", nodes_file),
                mock.patch.object(manager, "BLACKLIST_FILE", blacklist_file),
                mock.patch.object(manager, "active_openvpn_node_id", ""),
                mock.patch.object(manager, "reserved_slot_candidate_ids", return_value=set()),
            ):
                checked_at = time.time()
                changed = manager.mark_candidate_unavailable(
                    "jp-stale", "candidate_dial_failed", now=checked_at
                )
                reloaded = json.loads(nodes_file.read_text(encoding="utf-8"))
                candidates_after_restart = manager.safe_candidate_snapshot()

            blacklist = json.loads(blacklist_file.read_text(encoding="utf-8"))

        self.assertTrue(changed)
        self.assertEqual(reloaded[0]["probe_status"], "unavailable")
        self.assertEqual(reloaded[0]["probe_message"], "candidate_dial_failed")
        self.assertNotIn("exit_ip", reloaded[0])
        self.assertEqual(blacklist["jp-stale"]["reason_code"], "candidate_dial_failed")
        self.assertEqual(candidates_after_restart, [])

    def test_mark_candidate_unavailable_rejects_non_candidate_failures(self):
        with (
            mock.patch.object(manager, "read_nodes") as read,
            mock.patch.object(manager, "write_json") as write,
        ):
            changed = manager.mark_candidate_unavailable(
                "jp-one", "gateway_validation_failed", now=100.0
            )

        self.assertFalse(changed)
        read.assert_not_called()
        write.assert_not_called()

    def test_blacklist_remains_authoritative_when_nodes_write_crashes(self):
        candidate = {
            "id": "jp-crash",
            "country_short": "JP",
            "country": "Japan",
            "ip": "198.51.100.31",
            "ip_type": "hosting",
            "probe_status": "available",
            "config_file": "jp-crash.ovpn",
            "config_text": "client",
        }
        original_write_json = manager.write_json
        with tempfile.TemporaryDirectory() as directory:
            nodes_file = Path(directory) / "nodes.json"
            blacklist_file = Path(directory) / "blacklist.json"
            nodes_file.write_text(json.dumps([candidate]), encoding="utf-8")
            blacklist_file.write_text("{}", encoding="utf-8")

            def crash_after_blacklist(path, payload):
                if path == nodes_file:
                    raise OSError("simulated nodes write crash")
                return original_write_json(path, payload)

            with (
                mock.patch.object(manager, "NODES_FILE", nodes_file),
                mock.patch.object(manager, "BLACKLIST_FILE", blacklist_file),
                mock.patch.object(manager, "active_openvpn_node_id", ""),
                mock.patch.object(manager, "reserved_slot_candidate_ids", return_value=set()),
                mock.patch.object(manager, "write_json", side_effect=crash_after_blacklist),
            ):
                with self.assertRaises(OSError):
                    manager.mark_candidate_unavailable(
                        "jp-crash", "candidate_dial_failed", now=time.time()
                    )

            self.assertEqual(
                json.loads(nodes_file.read_text(encoding="utf-8"))[0]["probe_status"],
                "available",
            )
            with (
                mock.patch.object(manager, "NODES_FILE", nodes_file),
                mock.patch.object(manager, "BLACKLIST_FILE", blacklist_file),
                mock.patch.object(manager, "active_openvpn_node_id", ""),
                mock.patch.object(manager, "reserved_slot_candidate_ids", return_value=set()),
                mock.patch.object(manager, "main_reserved_candidate_ids", return_value=set()),
                mock.patch.object(manager, "get_slot_pin_map", return_value={}),
                mock.patch.object(manager, "get_exit_slot_config", return_value={"active": [2], "country": "JP", "residential_only": False}),
                mock.patch.object(manager, "get_active_slots", return_value=[2]),
                mock.patch.object(manager, "per_slot_country", return_value="JP"),
                mock.patch.object(manager, "per_slot_isp", return_value=""),
                mock.patch.object(manager, "per_slot_type", return_value="datacenter"),
            ):
                self.assertEqual(manager.safe_candidate_snapshot(), [])
                self.assertEqual(
                    manager.select_slot_nodes(set(), 1, "JP", False, proxy_type="datacenter"),
                    [],
                )
                self.assertIsNone(manager.pick_slot_node(2, set()))
                self.assertEqual(
                    manager.assign_node_to_slot(2, "jp-crash")["error"],
                    "未找到该节点",
                )
                self.assertEqual(
                    manager.add_slot_with_node("jp-crash")["error"],
                    "未找到该节点",
                )
                self.assertEqual(
                    manager.assign_managed_slot(2, "jp-crash", "JP", "datacenter")["error_code"],
                    "candidate_not_found",
                )

    def test_connect_node_does_not_reject_candidate_for_local_openvpn_failure(self):
        candidate = {
            "id": "jp-local-failure",
            "country_short": "JP",
            "country": "Japan",
            "ip": "198.51.100.32",
            "ip_type": "hosting",
            "probe_status": "available",
            "config_text": "client",
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            config_dir = Path(directory) / "configs"
            config_file = config_dir / "candidate.ovpn"
            candidate["config_file"] = str(config_file)
            for failure in (
                "OpenVPN log reader thread failed",
                "permission denied while opening TUN device",
                "cannot ioctl TUNSETIFF",
                "OpenVPN timeout after 12s",
            ):
                with self.subTest(failure=failure):
                    with (
                        mock.patch.object(manager, "DATA_DIR", data_dir),
                        mock.patch.object(manager, "CONFIG_DIR", config_dir),
                        mock.patch.object(manager, "is_connecting", False),
                        mock.patch.object(manager, "active_openvpn_node_id", ""),
                        mock.patch.object(manager, "read_nodes", return_value=[candidate]),
                        mock.patch.object(manager, "reserved_slot_candidate_ids", return_value=set()),
                        mock.patch.object(manager, "load_ui_config", return_value={}),
                        mock.patch.object(manager, "set_state"),
                        mock.patch.object(manager, "log_to_json"),
                        mock.patch.object(manager, "stop_active_openvpn"),
                        mock.patch.object(
                            manager,
                            "run_openvpn_until_ready",
                            return_value=(False, failure, None),
                        ),
                        mock.patch.object(manager, "mark_candidate_unavailable") as mark,
                    ):
                        with self.assertRaises(RuntimeError):
                            manager.connect_node("jp-local-failure")

                    mark.assert_not_called()

    def test_connect_node_rejects_candidate_for_explicit_remote_refusal(self):
        candidate = {
            "id": "jp-refused",
            "country_short": "JP",
            "country": "Japan",
            "ip": "198.51.100.33",
            "ip_type": "hosting",
            "probe_status": "available",
            "config_text": "client",
        }
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            config_dir = Path(directory) / "configs"
            candidate["config_file"] = str(config_dir / "candidate.ovpn")
            with (
                mock.patch.object(manager, "DATA_DIR", data_dir),
                mock.patch.object(manager, "CONFIG_DIR", config_dir),
                mock.patch.object(manager, "is_connecting", False),
                mock.patch.object(manager, "active_openvpn_node_id", ""),
                mock.patch.object(manager, "read_nodes", return_value=[candidate]),
                mock.patch.object(manager, "reserved_slot_candidate_ids", return_value=set()),
                mock.patch.object(manager, "load_ui_config", return_value={}),
                mock.patch.object(manager, "set_state"),
                mock.patch.object(manager, "log_to_json"),
                mock.patch.object(manager, "stop_active_openvpn"),
                mock.patch.object(
                    manager,
                    "run_openvpn_until_ready",
                    return_value=(False, "TCP connection refused", None),
                ),
                mock.patch.object(manager, "mark_candidate_unavailable", return_value=True) as mark,
            ):
                with self.assertRaises(manager.CandidateUnavailableError):
                    manager.connect_node("jp-refused")

        mark.assert_called_once_with("jp-refused", "candidate_dial_failed")

    def test_normalize_proxy_type_maps_only_supported_categories(self):
        cases = {
            "residential": "residential",
            "mobile": "residential",
            "hosting": "datacenter",
            "proxy": "datacenter",
            "": "",
            "unknown": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(manager.normalize_proxy_type(raw), expected)

    def test_safe_candidate_snapshot_excludes_unavailable_and_secret_fields(self):
        available = {
            "id": "node-ok",
            "country_short": "JP",
            "country": "Japan",
            "ip": "198.51.100.10",
            "ip_type": "mobile",
            "owner": "Example ISP",
            "asn": "AS64500",
            "as_name": "Example",
            "latency_ms": 42,
            "score": 123,
            "probe_status": "available",
            "last_probe_at": 1_700_000_000,
            "exit_ip": "203.0.113.10",
            "exit_ip_checked_at": 1_700_000_005,
            "config_text": "secret openvpn profile",
            "config_file": "secret.ovpn",
        }
        unavailable = dict(available, id="node-bad", probe_status="unavailable")

        with mock.patch.object(manager, "read_nodes", return_value=[available, unavailable]):
            actual = manager.safe_candidate_snapshot()

        self.assertEqual(
            actual,
            [
                {
                    "id": "node-ok",
                    "country_short": "JP",
                    "country": "Japan",
                    "ip": "198.51.100.10",
                    "proxy_type": "residential",
                    "owner": "Example ISP",
                    "asn": "AS64500",
                    "as_name": "Example",
                    "latency_ms": 42,
                    "score": 123,
                    "probe_status": "available",
                    "last_probe_at": 1_700_000_000,
                    "exit_ip": "203.0.113.10",
                    "exit_ip_checked_at": 1_700_000_005,
                }
            ],
        )

    def test_select_slot_nodes_enforces_country_and_proxy_type(self):
        nodes = [
            {"id": "jp-home", "country_short": "JP", "ip_type": "mobile", "probe_status": "available", "latency_ms": 20, "score": 2},
            {"id": "jp-dc", "country_short": "JP", "ip_type": "hosting", "probe_status": "available", "latency_ms": 10, "score": 3},
            {"id": "us-dc", "country_short": "US", "ip_type": "hosting", "probe_status": "available", "latency_ms": 5, "score": 4},
            {"id": "jp-unknown", "country_short": "JP", "ip_type": "unknown", "probe_status": "available", "latency_ms": 1, "score": 5},
        ]

        with mock.patch.object(manager, "read_nodes", return_value=nodes):
            residential = manager.select_slot_nodes(set(), 4, "JP", False, proxy_type="residential")
            datacenter = manager.select_slot_nodes(set(), 4, "JP", False, proxy_type="datacenter")

        self.assertEqual([item["id"] for item in residential], ["jp-home"])
        self.assertEqual([item["id"] for item in datacenter], ["jp-dc"])

    def test_rotate_slot_keeps_its_proxy_type_constraint(self):
        nodes = [
            {"id": "jp-home", "country_short": "JP", "country": "Japan", "ip": "198.51.100.20", "ip_type": "mobile", "probe_status": "available", "latency_ms": 1, "score": 9},
            {"id": "jp-dc", "country_short": "JP", "country": "Japan", "ip": "198.51.100.30", "ip_type": "hosting", "probe_status": "available", "latency_ms": 20, "score": 2},
        ]
        with (
            mock.patch.object(manager, "get_exit_slot_config", return_value={"active": [0], "paused": [], "residential_only": True}),
            mock.patch.object(manager, "set_slot_pin"),
            mock.patch.object(manager, "current_slot_node_ids", return_value={"old-node"}),
            mock.patch.object(manager, "read_nodes", return_value=nodes),
            mock.patch.object(manager, "per_slot_country", return_value="JP"),
            mock.patch.object(manager, "per_slot_isp", return_value=""),
            mock.patch.object(manager, "per_slot_type", return_value="datacenter"),
            mock.patch.object(manager, "tear_down_slot"),
            mock.patch.object(manager, "bring_up_slot", return_value=True),
            mock.patch.object(manager, "write_slots_state"),
        ):
            result = manager.switch_slot_node(0)

        self.assertTrue(result["ok"])
        self.assertEqual(result["ip"], "198.51.100.30")

    def test_supervisor_prefers_explicit_slot_type_over_global_residential_filter(self):
        nodes = [
            {"id": "jp-home", "country_short": "JP", "ip_type": "mobile", "probe_status": "available", "latency_ms": 1, "score": 9},
            {"id": "jp-dc", "country_short": "JP", "ip_type": "hosting", "probe_status": "available", "latency_ms": 20, "score": 2},
        ]
        with (
            mock.patch.object(manager, "get_slot_pin_map", return_value={}),
            mock.patch.object(manager, "get_exit_slot_config", return_value={"residential_only": True}),
            mock.patch.object(manager, "per_slot_country", return_value="JP"),
            mock.patch.object(manager, "per_slot_isp", return_value=""),
            mock.patch.object(manager, "per_slot_type", return_value="datacenter"),
            mock.patch.object(manager, "read_nodes", return_value=nodes),
        ):
            selected = manager.pick_slot_node(0, set())

        self.assertEqual(selected["id"], "jp-dc")

    def test_failed_rotate_candidate_enters_cooldown(self):
        candidate = {
            "id": "jp-stale",
            "country_short": "JP",
            "country": "Japan",
            "ip": "198.51.100.30",
            "ip_type": "hosting",
            "probe_status": "available",
            "latency_ms": 20,
            "score": 2,
        }
        bad_nodes = {}
        with (
            mock.patch.object(manager, "get_exit_slot_config", return_value={"active": [0], "paused": [], "residential_only": False}),
            mock.patch.object(manager, "set_slot_pin"),
            mock.patch.object(manager, "current_slot_node_ids", return_value={"old-node"}),
            mock.patch.object(manager, "read_nodes", return_value=[candidate]),
            mock.patch.object(manager, "per_slot_country", return_value="JP"),
            mock.patch.object(manager, "per_slot_isp", return_value=""),
            mock.patch.object(manager, "per_slot_type", return_value="datacenter"),
            mock.patch.object(manager, "tear_down_slot"),
            mock.patch.object(manager, "bring_up_slot", return_value=False),
            mock.patch.object(manager, "mark_slot_pending"),
            mock.patch.object(manager, "write_slots_state"),
            mock.patch.object(manager, "slot_bad_nodes", bad_nodes),
        ):
            result = manager.switch_slot_node(0)

        self.assertFalse(result["ok"])
        self.assertIn("jp-stale", bad_nodes)


class ManagedSlotFacadeTests(unittest.TestCase):
    def test_check_managed_slot_persists_proven_egress_failure(self):
        snapshot = {
            "ok": True,
            "slot": 2,
            "node_id": "jp-stale",
            "port": 17930,
            "status": "up",
        }
        runtime = {2: {"node_id": "jp-stale", "status": "up"}}
        with (
            mock.patch.object(manager, "managed_slot_snapshot", return_value=snapshot),
            mock.patch.object(manager, "check_slot_egress", return_value=(False, "")),
            mock.patch.object(manager, "check_interface_exit_ip", return_value=(False, "")),
            mock.patch.object(manager, "slot_process_alive", return_value=True),
            mock.patch.object(manager, "mark_candidate_unavailable", return_value=True) as mark,
            mock.patch.object(manager, "exit_slots", runtime),
            mock.patch.object(manager, "write_slots_state"),
        ):
            result = manager.check_managed_slot(2)

        mark.assert_called_once_with("jp-stale", "candidate_egress_failed")
        self.assertEqual(result["error_code"], "candidate_egress_failed")
        self.assertTrue(result["candidate_rejected"])

    def test_check_managed_slot_keeps_candidate_when_tunnel_egress_is_healthy(self):
        snapshot = {
            "ok": True,
            "slot": 2,
            "node_id": "jp-live",
            "port": 17930,
            "status": "up",
        }
        with (
            mock.patch.object(manager, "managed_slot_snapshot", return_value=snapshot),
            mock.patch.object(manager, "check_slot_egress", return_value=(False, "")),
            mock.patch.object(
                manager,
                "check_interface_exit_ip",
                return_value=(True, "203.0.113.40"),
            ),
            mock.patch.object(manager, "slot_process_alive", return_value=True),
            mock.patch.object(manager, "mark_candidate_unavailable") as mark,
            mock.patch.object(manager, "exit_slots", {2: {"node_id": "jp-live"}}),
            mock.patch.object(manager, "write_slots_state"),
        ):
            result = manager.check_managed_slot(2)

        self.assertEqual(result["error_code"], "egress_check_failed")
        self.assertFalse(result["candidate_rejected"])
        mark.assert_not_called()

    def test_assign_managed_slot_propagates_proven_candidate_failure(self):
        candidate = {
            "id": "jp-stale",
            "country_short": "JP",
            "country": "Japan",
            "ip_type": "hosting",
            "probe_status": "available",
        }
        previous = {"node_id": "", "status": "pending"}
        with (
            mock.patch.object(manager, "get_active_slots", return_value=[2]),
            mock.patch.object(manager, "read_nodes", return_value=[candidate]),
            mock.patch.object(manager, "reserved_slot_candidate_ids", return_value=set()),
            mock.patch.object(manager, "exit_slots", {2: previous}),
            mock.patch.object(manager, "get_slot_pin_map", return_value={}),
            mock.patch.object(manager, "get_slot_country_map", return_value={}),
            mock.patch.object(manager, "get_slot_type_map", return_value={}),
            mock.patch.object(manager, "set_slot_country"),
            mock.patch.object(manager, "set_slot_type"),
            mock.patch.object(
                manager,
                "assign_node_to_slot",
                return_value={
                    "ok": False,
                    "error_code": "candidate_dial_failed",
                    "candidate_rejected": True,
                },
            ),
        ):
            result = manager.assign_managed_slot(2, "jp-stale", "JP", "datacenter")

        self.assertEqual(result["error_code"], "candidate_dial_failed")
        self.assertTrue(result["candidate_rejected"])

    def test_assign_managed_slot_preserves_rejection_when_restore_fails(self):
        candidate = {
            "id": "jp-stale",
            "country_short": "JP",
            "country": "Japan",
            "ip_type": "hosting",
            "probe_status": "available",
        }
        previous = {"node_id": "jp-old", "status": "up"}
        with (
            mock.patch.object(manager, "get_active_slots", return_value=[2]),
            mock.patch.object(manager, "read_nodes", return_value=[candidate]),
            mock.patch.object(manager, "reserved_slot_candidate_ids", return_value=set()),
            mock.patch.object(manager, "exit_slots", {2: previous}),
            mock.patch.object(manager, "get_slot_pin_map", return_value={"2": "jp-old"}),
            mock.patch.object(manager, "get_slot_country_map", return_value={"2": "US"}),
            mock.patch.object(manager, "get_slot_type_map", return_value={"2": "datacenter"}),
            mock.patch.object(manager, "set_slot_country"),
            mock.patch.object(manager, "set_slot_type"),
            mock.patch.object(
                manager,
                "assign_node_to_slot",
                side_effect=[
                    {
                        "ok": False,
                        "error_code": "candidate_dial_failed",
                        "candidate_rejected": True,
                    },
                    {"ok": False, "error_code": "assign_failed"},
                ],
            ),
        ):
            result = manager.assign_managed_slot(2, "jp-stale", "JP", "datacenter")

        self.assertEqual(
            result,
            {
                "ok": False,
                "state": "repair_required",
                "error_code": "rollback_failed",
                "candidate_rejected": True,
            },
        )

    def test_slot_orphan_cleanup_matches_only_the_exact_managed_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            commands = {
                101: ["/usr/sbin/openvpn", "--setenv", "AIMILI_SLOT", "2"],
                102: ["/usr/sbin/openvpn", "--setenv", "AIMILI_SLOT", "1"],
                103: ["/usr/sbin/openvpn", "--setenv", "NOT_AIMILI_SLOT", "2"],
            }
            for pid, command in commands.items():
                path = proc_root / str(pid)
                path.mkdir()
                (path / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")
            killed = []
            with (
                mock.patch.object(manager.sys, "platform", "linux"),
                mock.patch.object(manager.os, "kill", side_effect=lambda pid, sig: killed.append((pid, sig))),
                mock.patch.object(manager.time, "sleep"),
            ):
                manager.kill_unregistered_slot_openvpn_processes(2, proc_root=proc_root)

        self.assertEqual([pid for pid, _sig in killed], [101, 101])

    def test_openvpn_reader_thread_failure_reaps_started_process(self):
        class Process:
            stdout = []

            def __init__(self):
                self.terminated = False
                self.waited = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                self.waited = True
                return 0

        class Thread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        process = Process()
        with (
            mock.patch.object(manager.subprocess, "Popen", return_value=process),
            mock.patch.object(manager.threading, "Thread", Thread),
        ):
            ok, message, returned = manager.run_openvpn_until_ready(
                "slot.ovpn", True, True, timeout=1, dev="tun122"
            )

        self.assertFalse(ok)
        self.assertIn("thread", message.lower())
        self.assertIsNone(returned)
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)

    def test_policy_routing_failure_reaps_process_and_does_not_register_slot(self):
        process = mock.Mock()
        node = {
            "id": "jp-one", "country": "Japan", "country_short": "JP",
            "ip": "198.51.100.10", "ip_type": "hosting", "config_text": "client",
        }
        runtime = {}
        with (
            mock.patch.object(manager, "CONFIG_DIR"),
            mock.patch.object(manager, "slot_config_path", return_value=mock.MagicMock()),
            mock.patch.object(manager, "run_openvpn_until_ready", return_value=(True, "ok", process)),
            mock.patch.object(manager, "setup_policy_routing", return_value=False),
            mock.patch.object(manager, "stop_process") as stop,
            mock.patch.object(manager, "ensure_slot_proxy") as ensure_proxy,
            mock.patch.object(manager, "exit_slots", runtime),
        ):
            result = manager.bring_up_slot(2, node)

        self.assertFalse(result)
        stop.assert_called_once_with(process)
        ensure_proxy.assert_not_called()
        self.assertNotIn(2, runtime)

    def test_create_managed_slot_pins_the_requested_candidate(self):
        candidates = [
            {"id": "jp-fast", "country_short": "JP", "country": "Japan", "ip_type": "hosting", "probe_status": "available", "latency_ms": 1},
            {"id": "jp-requested", "country_short": "JP", "country": "Japan", "ip_type": "hosting", "probe_status": "available", "latency_ms": 20},
        ]
        selected = []
        runtime_slot = {
            "slot": 3, "country_short": "JP", "country": "Japan", "ip_type": "hosting",
            "port": 17931, "status": "up", "node_id": "jp-requested", "process": object(),
        }
        with (
            mock.patch.object(manager, "read_nodes", return_value=candidates),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "add_slot_with_node", side_effect=lambda node: selected.append(node) or {"ok": True, "slot": 3}),
            mock.patch.object(manager, "set_slot_country"),
            mock.patch.object(manager, "set_slot_type"),
            mock.patch.object(manager, "get_slot_country_map", return_value={"3": "JP"}),
            mock.patch.object(manager, "get_slot_type_map", return_value={"3": "datacenter"}),
            mock.patch.object(manager, "exit_slots", {3: runtime_slot}),
        ):
            result = manager.create_managed_slot("JP", "datacenter", "jp-requested")

        self.assertEqual(selected, ["jp-requested"])
        self.assertEqual(result["node_id"], "jp-requested")

    def test_create_managed_slot_rejects_requested_candidate_with_wrong_classification(self):
        candidate = {
            "id": "kr-home", "country_short": "KR", "country": "Korea",
            "ip_type": "residential", "probe_status": "available",
        }
        with mock.patch.object(manager, "read_nodes", return_value=[candidate]):
            result = manager.create_managed_slot("JP", "datacenter", "kr-home")

        self.assertEqual(result, {"ok": False, "error_code": "candidate_mismatch"})

    def test_managed_slots_snapshot_exposes_safe_runtime_fields_only(self):
        runtime = {
            1: {
                "slot": 1, "country_short": "JP", "country": "Japan", "ip_type": "hosting",
                "port": 17929, "status": "up", "node_id": "jp-one", "process": object(),
                "config_text": "secret",
            }
        }
        with (
            mock.patch.object(manager, "exit_slots", runtime),
            mock.patch.object(manager, "get_slot_country_map", return_value={"1": "JP"}),
            mock.patch.object(manager, "get_slot_type_map", return_value={"1": "datacenter"}),
        ):
            snapshots = manager.managed_slots_snapshot()

        self.assertEqual([item["slot"] for item in snapshots], [1])
        self.assertNotIn("process", snapshots[0])
        self.assertNotIn("config_text", snapshots[0])

    def test_start_control_plane_uses_explicit_loopback_configuration(self):
        sentinel = object()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "AIMILI_CONTROL_ADDRESS": "127.0.0.1:8899",
                    "AIMILI_CONTROL_TOKEN_FILE": "/run/aimili/control.token",
                },
                clear=False,
            ),
            mock.patch("control_api.start_control_server", return_value=sentinel) as start,
        ):
            actual = manager.start_control_plane()

        self.assertIs(actual, sentinel)
        args = start.call_args.args
        self.assertIs(args[0], manager)
        self.assertEqual(args[1], "127.0.0.1:8899")
        self.assertEqual(str(args[2]).replace("\\", "/"), "/run/aimili/control.token")

    def test_create_managed_slot_selects_matching_node_and_returns_safe_snapshot(self):
        candidates = [
            {"id": "jp-home", "country_short": "JP", "country": "Japan", "ip": "198.51.100.20", "ip_type": "mobile", "probe_status": "available", "latency_ms": 1, "score": 9},
            {"id": "jp-dc", "country_short": "JP", "country": "Japan", "ip": "198.51.100.30", "ip_type": "hosting", "probe_status": "available", "latency_ms": 20, "score": 2},
        ]
        runtime_slot = {
            "slot": 2,
            "country_short": "JP",
            "country": "Japan",
            "ip": "198.51.100.30",
            "ip_type": "hosting",
            "port": 17930,
            "status": "up",
            "node_id": "jp-dc",
            "process": object(),
        }
        selected = []
        countries = {}
        types = {}

        def add(node_id):
            selected.append(node_id)
            return {"ok": True, "slot": 2}

        with (
            mock.patch.object(manager, "read_nodes", return_value=candidates),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "add_slot_with_node", side_effect=add),
            mock.patch.object(manager, "set_slot_country", side_effect=lambda slot, value: countries.update({str(slot): value}) or countries.copy()),
            mock.patch.object(manager, "set_slot_type", side_effect=lambda slot, value: types.update({str(slot): value}) or types.copy()),
            mock.patch.object(manager, "get_slot_country_map", side_effect=lambda: countries.copy()),
            mock.patch.object(manager, "get_slot_type_map", side_effect=lambda: types.copy()),
            mock.patch.object(manager, "exit_slots", {2: runtime_slot}),
        ):
            result = manager.create_managed_slot("JP", "datacenter")

        self.assertEqual(selected, ["jp-dc"])
        self.assertEqual(result["proxy_type"], "datacenter")
        self.assertEqual(result["port"], 17930)
        self.assertNotIn("process", result)

    def test_create_managed_slot_reports_no_candidate_without_allocating(self):
        allocated = []
        with (
            mock.patch.object(manager, "read_nodes", return_value=[]),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "add_slot_with_node", side_effect=lambda node: allocated.append(node)),
        ):
            result = manager.create_managed_slot("JP", "datacenter")

        self.assertEqual(result, {"ok": False, "error_code": "no_matching_candidate"})
        self.assertEqual(allocated, [])

    def test_create_managed_slot_retries_the_next_matching_candidate(self):
        candidates = [
            {"id": "jp-stale", "country_short": "JP", "country": "Japan", "ip_type": "hosting", "probe_status": "available", "latency_ms": 1, "score": 9},
            {"id": "jp-live", "country_short": "JP", "country": "Japan", "ip_type": "hosting", "probe_status": "available", "latency_ms": 2, "score": 8},
        ]
        runtime_slot = {
            "slot": 0,
            "country_short": "JP",
            "country": "Japan",
            "ip_type": "hosting",
            "port": 17928,
            "status": "up",
            "node_id": "jp-live",
            "process": object(),
        }
        selected = []
        countries = {}
        types = {}

        def add(node_id):
            selected.append(node_id)
            if node_id == "jp-stale":
                return {"ok": False, "error": "authentication failed"}
            return {"ok": True, "slot": 0}

        with (
            mock.patch.object(manager, "read_nodes", return_value=candidates),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "add_slot_with_node", side_effect=add),
            mock.patch.object(manager, "set_slot_country", side_effect=lambda slot, value: countries.update({str(slot): value}) or countries.copy()),
            mock.patch.object(manager, "set_slot_type", side_effect=lambda slot, value: types.update({str(slot): value}) or types.copy()),
            mock.patch.object(manager, "get_slot_country_map", side_effect=lambda: countries.copy()),
            mock.patch.object(manager, "get_slot_type_map", side_effect=lambda: types.copy()),
            mock.patch.object(manager, "exit_slots", {0: runtime_slot}),
            mock.patch.object(manager, "slot_bad_nodes", {}),
        ):
            result = manager.create_managed_slot("JP", "datacenter")

        self.assertEqual(selected, ["jp-stale", "jp-live"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["node_id"], "jp-live")

    def test_add_slot_with_node_rolls_back_allocation_when_dial_fails(self):
        candidate = {"id": "jp-stale", "probe_status": "available"}
        with (
            mock.patch.object(manager, "read_nodes", return_value=[candidate]),
            mock.patch.object(manager, "get_active_slots", return_value=[]),
            mock.patch.object(manager, "load_ui_config", return_value={}),
            mock.patch.object(manager, "_save_slot_lists"),
            mock.patch.object(manager, "set_slot_pin"),
            mock.patch.object(manager, "assign_node_to_slot", return_value={"ok": False, "error": "authentication failed"}),
            mock.patch.object(manager, "delete_slot", return_value={"ok": True}) as delete,
        ):
            result = manager.add_slot_with_node("jp-stale")

        self.assertFalse(result["ok"])
        delete.assert_called_once_with(0)

    def test_rotate_managed_slot_retries_after_a_stale_candidate(self):
        expected = {"ok": True, "slot": 0, "node_id": "jp-live", "status": "up"}
        with (
            mock.patch.object(
                manager,
                "switch_slot_node",
                side_effect=[
                    {"ok": False, "error": "authentication failed"},
                    {"ok": True, "ip": "198.51.100.40"},
                ],
            ) as switch,
            mock.patch.object(manager, "managed_slot_snapshot", return_value=expected),
        ):
            result = manager.rotate_managed_slot(0)

        self.assertEqual(result, expected)
        self.assertEqual(switch.call_count, 2)

    def test_managed_rotate_waits_for_a_busy_slot_supervisor(self):
        manager.exit_slots_supervise_lock.acquire()
        release = threading.Timer(0.05, manager.exit_slots_supervise_lock.release)
        release.start()
        try:
            with (
                mock.patch.object(manager, "get_exit_slot_config", return_value={"active": [0], "paused": [], "residential_only": False}),
                mock.patch.object(manager, "set_slot_pin"),
                mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
                mock.patch.object(manager, "per_slot_country", return_value="JP"),
                mock.patch.object(manager, "per_slot_isp", return_value=""),
                mock.patch.object(manager, "per_slot_type", return_value="datacenter"),
                mock.patch.object(manager, "select_slot_nodes", return_value=[{"id": "jp-live", "ip": "198.51.100.40", "country": "Japan"}]),
                mock.patch.object(manager, "tear_down_slot"),
                mock.patch.object(manager, "bring_up_slot", return_value=True),
                mock.patch.object(manager, "write_slots_state"),
                mock.patch.object(manager, "managed_slot_snapshot", return_value={"ok": True, "slot": 0, "status": "up"}),
            ):
                started = time.monotonic()
                result = manager.rotate_managed_slot(0)
        finally:
            release.cancel()
            if manager.exit_slots_supervise_lock.locked():
                manager.exit_slots_supervise_lock.release()

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(time.monotonic() - started, 0.04)


if __name__ == "__main__":
    unittest.main()
