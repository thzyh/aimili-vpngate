import base64
import unittest
from unittest import mock

import vpngate_manager as manager


def node(index, status="not_checked", active=False):
    return {
        "id": f"n{index}",
        "probe_status": status,
        "active": active,
        "config_file": f"n{index}.ovpn",
        "config_text": "",
    }


class PoolMaintenanceTests(unittest.TestCase):
    def test_storage_sort_excludes_unavailable_and_untested_nodes(self):
        manager.active_openvpn_node_id = "live"
        try:
            stored = manager.sort_all_nodes(
                [
                    node(1, "available"),
                    node(2, "unavailable"),
                    node(3, "not_checked"),
                    {**node(4, "unavailable", True), "id": "live"},
                ]
            )
        finally:
            manager.active_openvpn_node_id = ""
        self.assertEqual([item["id"] for item in stored], ["n1", "live"])

    def test_replenishes_from_later_candidates_until_target(self):
        existing = [
            node(0, "available"),
            node(1, "available"),
            node(2, "unavailable"),
            node(3, "unavailable"),
        ]
        candidates = [node(index) for index in range(10)]
        calls = []

        def probe(batch):
            calls.append([item["id"] for item in batch])
            return [
                dict(
                    item,
                    probe_status=(
                        "unavailable" if item["id"] in {"n8", "n9"} else "available"
                    ),
                )
                for item in batch
            ]

        with (
            mock.patch.object(manager, "TARGET_VALID_POOL_SIZE", 5),
            mock.patch.object(manager, "NODE_TEST_BATCH_SIZE", 2),
        ):
            pool, blacklist, stats = manager.replenish_valid_pool(
                existing, candidates, {}, probe, now=100.0
            )

        self.assertEqual(
            [item["id"] for item in pool],
            ["n0", "n1", "n4", "n5", "n6"],
        )
        self.assertEqual(
            calls,
            [["n0", "n1"], ["n4", "n5"], ["n6", "n7"]],
        )
        self.assertEqual(set(blacklist), {"n2", "n3"})
        self.assertEqual(stats["stop_reason"], "target_reached")

    def test_stops_when_candidates_are_exhausted(self):
        def fail(batch):
            return [
                dict(item, probe_status="unavailable", probe_message="failed")
                for item in batch
            ]

        with (
            mock.patch.object(manager, "TARGET_VALID_POOL_SIZE", 3),
            mock.patch.object(manager, "NODE_TEST_BATCH_SIZE", 2),
        ):
            pool, _, stats = manager.replenish_valid_pool(
                [], [node(1), node(2)], {}, fail, now=100.0
            )
        self.assertEqual(pool, [])
        self.assertEqual(stats["tested"], 2)
        self.assertEqual(stats["stop_reason"], "candidates_exhausted")

    def test_missing_probe_result_is_cooled_and_not_retried(self):
        calls = []

        def omit(batch):
            calls.append([item["id"] for item in batch])
            return []

        with (
            mock.patch.object(manager, "TARGET_VALID_POOL_SIZE", 1),
            mock.patch.object(manager, "NODE_TEST_BATCH_SIZE", 1),
        ):
            pool, blacklist, stats = manager.replenish_valid_pool(
                [], [node(1)], {}, omit, now=100.0
            )
        self.assertEqual(pool, [])
        self.assertEqual(calls, [["n1"]])
        self.assertIn("n1", blacklist)
        self.assertEqual(stats["tested"], 1)

    def test_api_failure_keeps_the_existing_pool(self):
        existing = [node(1, "available")]
        with (
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "read_nodes", return_value=existing),
            mock.patch.object(
                manager, "fetch_candidates", side_effect=RuntimeError("API down")
            ),
            mock.patch.object(manager.vpn_utils, "check_and_fix_dns"),
            mock.patch.object(
                manager.vpn_utils,
                "diagnose_api_failure",
                return_value=(1000, "API down"),
            ),
            mock.patch.object(manager, "set_state"),
            mock.patch.object(manager, "write_json") as write,
        ):
            message = manager.maintain_valid_nodes()
        self.assertIn("保留现有有效节点", message)
        write.assert_not_called()


class FetchCandidatesTests(unittest.TestCase):
    @staticmethod
    def api_text(count):
        header = (
            "#HostName,IP,CountryShort,CountryLong,Score,Ping,Speed,"
            "NumVpnSessions,OpenVPN_ConfigData_Base64"
        )
        rows = []
        for index in range(count):
            ip = f"198.51.100.{index + 1}"
            config = base64.b64encode(
                f"remote {ip} 443 tcp\nproto tcp\n".encode("utf-8")
            ).decode("ascii")
            rows.append(f"host-{index},{ip},US,United States,100,10,1000,1,{config}")
        return "\n".join([header, *rows])

    def test_fetch_reads_beyond_the_first_thirty_rows(self):
        with (
            mock.patch.object(manager, "MAX_FETCH_ROWS", 40),
            mock.patch.object(manager, "load_blacklist", return_value={}),
            mock.patch.object(manager, "cached_nodes", return_value=[]),
            mock.patch.object(manager, "fetch_api_text", return_value=self.api_text(40)),
            mock.patch.object(manager, "set_state"),
            mock.patch.object(manager, "log_to_json"),
        ):
            candidates = manager.fetch_candidates()
        self.assertEqual(len(candidates), 40)

    def test_successful_empty_snapshot_does_not_report_api_failure(self):
        cooling = {
            "US_198.51.100.1_443_tcp": {"until": 9999999999.0},
            "US_198.51.100.2_443_tcp": {"until": 9999999999.0},
        }
        with (
            mock.patch.object(manager, "MAX_FETCH_ROWS", 2),
            mock.patch.object(manager, "load_blacklist", return_value=cooling),
            mock.patch.object(manager, "cached_nodes", return_value=[]),
            mock.patch.object(manager, "fetch_api_text", return_value=self.api_text(2)),
            mock.patch.object(manager, "set_state"),
            mock.patch.object(manager, "log_to_json"),
            mock.patch.object(manager.vpn_utils, "diagnose_api_failure") as diagnose,
        ):
            candidates = manager.fetch_candidates()
        self.assertEqual(candidates, [])
        diagnose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
