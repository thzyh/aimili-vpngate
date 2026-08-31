import base64
import tempfile
import time
import unittest
from pathlib import Path
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


def country_node(node_id, country, status="not_checked"):
    return {
        "id": node_id,
        "country_short": country,
        "country": country,
        "probe_status": status,
        "config_file": f"{node_id}.ovpn",
        "config_text": f"remote {node_id}.invalid 443 tcp\n",
    }


class PoolMaintenanceTests(unittest.TestCase):
    def test_corrupt_pool_metadata_returns_safe_defaults_without_touching_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "pool_metadata.json"
            metadata.write_text("{broken", encoding="utf-8")
            with mock.patch.object(manager, "POOL_METADATA_FILE", metadata):
                result = manager.load_pool_metadata()

        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["maintenanceRound"], 0)
        self.assertEqual(result["manualProtectedIds"], [])

    def test_country_refresh_runs_in_background_and_exposes_safe_status(self):
        candidates = [country_node("jp-one", "JP")]
        with (
            mock.patch.object(manager, "read_nodes", return_value=[]),
            mock.patch.object(manager, "fetch_candidates", return_value=candidates),
            mock.patch.object(
                manager,
                "country_catalog_snapshot",
                return_value=[
                    {"code": "JP", "candidateCount": 1},
                    {"code": "US", "candidateCount": 2},
                ],
            ),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(
                manager,
                "probe_nodes",
                side_effect=lambda batch: [dict(item, probe_status="available") for item in batch],
            ),
            mock.patch.object(manager, "load_blacklist", return_value={}),
            mock.patch.object(manager, "write_json"),
            mock.patch.object(manager, "set_state"),
        ):
            accepted = manager.start_country_refresh("jp")
            deadline = time.time() + 2
            snapshot = manager.country_refresh_snapshot()
            while snapshot["state"] == "running" and time.time() < deadline:
                time.sleep(0.01)
                snapshot = manager.country_refresh_snapshot()

        self.assertEqual(accepted["state"], "running")
        self.assertEqual(snapshot["state"], "completed")
        self.assertEqual(snapshot["country"], "JP")
        self.assertEqual(snapshot["catalogCount"], 3)
        self.assertEqual(snapshot["countryCandidateCount"], 1)
        self.assertEqual(snapshot["testedCount"], 1)
        self.assertNotIn("exception", snapshot)

    def test_country_refresh_preserves_other_countries_and_managed_slots(self):
        existing = [
            country_node("jp-old", "JP", "available"),
            country_node("jp-slot", "JP", "available"),
            country_node("us-existing", "US", "available"),
        ]
        candidates = [country_node(f"jp-new-{index}", "JP") for index in range(8)]
        stored_nodes = []

        def probe(batch):
            return [dict(item, probe_status="available") for item in batch]

        def store(path, payload):
            if path == manager.NODES_FILE:
                stored_nodes[:] = payload

        with (
            mock.patch.object(manager, "read_nodes", return_value=existing),
            mock.patch.object(manager, "fetch_candidates", return_value=candidates),
            mock.patch.object(manager, "current_slot_node_ids", return_value={"jp-slot"}),
            mock.patch.object(manager, "probe_nodes", side_effect=probe),
            mock.patch.object(manager, "load_blacklist", return_value={}),
            mock.patch.object(manager, "write_json", side_effect=store),
            mock.patch.object(manager, "set_state"),
        ):
            result = manager.refresh_country_nodes("JP", target_size=5, max_probes=20)

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["testedCount"], 4)
        self.assertEqual(result["validCount"], 6)
        self.assertEqual(
            [item["id"] for item in stored_nodes],
            ["jp-slot", "jp-new-0", "jp-new-1", "jp-new-2", "jp-new-3", "us-existing", "jp-old"],
        )

    def test_country_refresh_stops_after_twenty_failed_real_probes(self):
        candidates = [country_node(f"jp-{index}", "JP") for index in range(30)]

        def fail(batch):
            return [
                dict(item, probe_status="unavailable", probe_message="dial_failed")
                for item in batch
            ]

        with (
            mock.patch.object(manager, "read_nodes", return_value=[]),
            mock.patch.object(manager, "fetch_candidates", return_value=candidates),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "probe_nodes", side_effect=fail),
            mock.patch.object(manager, "load_blacklist", return_value={}),
            mock.patch.object(manager, "write_json"),
            mock.patch.object(manager, "set_state"),
        ):
            result = manager.refresh_country_nodes("JP", target_size=5, max_probes=20)

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["testedCount"], 20)
        self.assertEqual(result["validCount"], 0)
        self.assertEqual(result["stopReason"], "probe_limit_reached")

    def test_collector_delays_boot_scan_and_backs_off_after_failure(self):
        sleeps = []

        class StopLoop(Exception):
            pass

        def record_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise StopLoop()

        with (
            mock.patch.object(manager, "COLLECTOR_INITIAL_DELAY_SECONDS", 120),
            mock.patch.object(manager, "COLLECTOR_FAILURE_BACKOFF_SECONDS", 600),
            mock.patch.object(manager, "maintain_valid_nodes", return_value="获取节点失败"),
            mock.patch.object(manager, "active_openvpn_running", return_value=False),
            mock.patch.object(manager, "log_to_json"),
            mock.patch.object(manager.time, "sleep", side_effect=record_sleep),
        ):
            with self.assertRaises(StopLoop):
                manager.collector_loop()

        self.assertEqual(sleeps, [120, 600])

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
    def api_text(count, countries=None):
        header = (
            "#HostName,IP,CountryShort,CountryLong,Score,Ping,Speed,"
            "NumVpnSessions,OpenVPN_ConfigData_Base64"
        )
        rows = []
        for index in range(count):
            ip = f"198.51.100.{index + 1}"
            country = (countries or ["US"] * count)[index]
            config = base64.b64encode(
                f"remote {ip} 443 tcp\nproto tcp\n".encode("utf-8")
            ).decode("ascii")
            country_long = "Japan" if country == "JP" else "United States"
            rows.append(f"host-{index},{ip},{country},{country_long},100,10,1000,1,{config}")
        return "\n".join([header, *rows])

    def test_country_fetch_filters_before_the_candidate_limit(self):
        text = self.api_text(5, ["US", "US", "JP", "JP", "JP"])
        with (
            mock.patch.object(manager, "MAX_FETCH_ROWS", 2),
            mock.patch.object(manager, "load_blacklist", return_value={}),
            mock.patch.object(manager, "cached_nodes", return_value=[]),
            mock.patch.object(manager, "fetch_api_text", return_value=text),
            mock.patch.object(manager, "set_state"),
            mock.patch.object(manager, "log_to_json"),
            mock.patch.object(manager, "store_country_catalog"),
        ):
            candidates = manager.fetch_candidates("JP")

        self.assertEqual([item["country_short"] for item in candidates], ["JP", "JP"])

    def test_country_catalog_counts_raw_rows_without_node_secrets(self):
        rows = manager.parse_vpngate_rows(
            self.api_text(3, ["JP", "US", "JP"])
        )

        catalog = manager.build_country_catalog(rows, observed_at=100.0)

        self.assertEqual(
            catalog,
            [
                {"code": "JP", "name": "日本", "candidateCount": 2, "observedAt": 100.0},
                {"code": "US", "name": "美国", "candidateCount": 1, "observedAt": 100.0},
            ],
        )
        self.assertNotIn("OpenVPN_ConfigData_Base64", str(catalog))

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
