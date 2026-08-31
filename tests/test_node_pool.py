import unittest

from node_pool import (
    candidate_queue,
    filter_country_rows,
    merge_country_pool,
    merge_probe_results,
    protected_active_nodes,
    protected_node_ids,
    rebalance_valid_pool,
)


def node(node_id, status="not_checked", active=False):
    return {"id": node_id, "probe_status": status, "active": active}


class NodePoolTests(unittest.TestCase):
    def test_rebalance_is_stable_keeps_country_anchors_and_never_exceeds_limit(self):
        existing = [
            {**node("jp-old", "available"), "country_short": "JP", "ip_type": "hosting", "latency_ms": 50},
            {**node("us-old", "available"), "country_short": "US", "ip_type": "hosting", "latency_ms": 60},
        ]
        refreshed = [
            {**node("jp-home", "available"), "country_short": "JP", "ip_type": "residential", "latency_ms": 10},
            {**node("de-home", "available"), "country_short": "DE", "ip_type": "mobile", "latency_ms": 20},
            {**node("bad", "unavailable"), "country_short": "FR", "ip_type": "hosting", "latency_ms": 1},
        ]

        first = rebalance_valid_pool(existing, refreshed, set(), set(), limit=3)
        second = rebalance_valid_pool(list(reversed(existing)), list(reversed(refreshed)), set(), set(), limit=3)

        self.assertEqual([item["id"] for item in first], ["jp-old", "us-old", "de-home"])
        self.assertEqual([item["id"] for item in second], ["jp-old", "us-old", "de-home"])

    def test_rebalance_hard_protects_runtime_and_prefers_manual_success(self):
        existing = [
            {**node("runtime", "unavailable"), "country_short": "JP"},
            {**node("old", "available"), "country_short": "US"},
        ]
        refreshed = [
            {**node("manual", "available"), "country_short": "DE", "ip_type": "hosting"},
            {**node("fresh", "available"), "country_short": "FR", "ip_type": "residential"},
        ]

        result = rebalance_valid_pool(existing, refreshed, {"runtime"}, {"manual"}, limit=3)

        self.assertEqual([item["id"] for item in result], ["runtime", "manual", "old"])

    def test_country_filter_runs_on_raw_rows_without_global_truncation(self):
        rows = [
            {"IP": "192.0.2.1", "CountryShort": "US"},
            {"IP": "192.0.2.2", "CountryShort": "jp"},
            {"IP": "192.0.2.3", "CountryShort": "JP"},
        ]

        self.assertEqual(
            [row["IP"] for row in filter_country_rows(rows, "JP")],
            ["192.0.2.2", "192.0.2.3"],
        )

    def test_protected_node_ids_include_main_and_every_managed_slot(self):
        self.assertEqual(
            protected_node_ids("main-node", ["slot-2", "", "slot-1", "slot-2"]),
            {"main-node", "slot-1", "slot-2"},
        )

    def test_country_merge_preserves_other_countries_and_protected_nodes(self):
        existing = [
            {**node("jp-old", "available"), "country_short": "JP"},
            {**node("jp-active", "unavailable"), "country_short": "JP"},
            {**node("us-existing", "available"), "country_short": "US"},
        ]
        refreshed = [
            {**node("jp-new", "available"), "country_short": "JP"},
            {**node("jp-new", "available"), "country_short": "JP"},
            {**node("jp-dead", "unavailable"), "country_short": "JP"},
        ]

        merged = merge_country_pool(
            existing,
            refreshed,
            "JP",
            {"jp-active"},
            target_size=5,
        )

        self.assertEqual(
            [item["id"] for item in merged],
            ["us-existing", "jp-active", "jp-new"],
        )

    def test_protects_only_the_current_active_node(self):
        existing = [
            node("good", "available"),
            node("bad", "unavailable"),
            node("live", "not_checked", True),
        ]
        self.assertEqual(
            [item["id"] for item in protected_active_nodes(existing, "live")],
            ["live"],
        )

    def test_stale_active_flag_is_not_protected_without_runtime_active_id(self):
        self.assertEqual(
            protected_active_nodes([node("stale", "available", True)], ""),
            [],
        )

    def test_candidate_queue_prioritizes_existing_valid_and_excludes_ineligible_nodes(self):
        candidates = [node("fresh"), node("good"), node("cool"), node("tested")]
        queue = candidate_queue(
            candidates,
            [],
            {"cool": {"until": 200.0}},
            {"tested"},
            now=100.0,
            preferred_ids={"good"},
        )
        self.assertEqual([item["id"] for item in queue], ["good", "fresh"])

    def test_expired_cooldown_is_eligible(self):
        queue = candidate_queue(
            [node("retry")],
            [],
            {"retry": {"until": 99.0}},
            set(),
            now=100.0,
            preferred_ids=set(),
        )
        self.assertEqual([item["id"] for item in queue], ["retry"])

    def test_merge_keeps_successes_returns_failures_and_stops_at_target(self):
        pool, failed = merge_probe_results(
            [node("old", "available")],
            [
                node("new-1", "available"),
                node("dead", "unavailable"),
                node("new-2", "available"),
            ],
            target_size=2,
        )
        self.assertEqual([item["id"] for item in pool], ["old", "new-1"])
        self.assertEqual([item["id"] for item in failed], ["dead"])


if __name__ == "__main__":
    unittest.main()
