import unittest
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
            mock.patch.object(manager, "get_exit_slot_config", return_value={"active": [0], "paused": [], "residential_only": False}),
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


class ManagedSlotFacadeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
