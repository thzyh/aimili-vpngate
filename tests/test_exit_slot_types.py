import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vpngate_manager as manager


class ExitSlotTypeTests(unittest.TestCase):
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
