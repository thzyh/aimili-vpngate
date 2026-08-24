import unittest

from node_pool import candidate_queue, merge_probe_results, protected_active_nodes


def node(node_id, status="not_checked", active=False):
    return {"id": node_id, "probe_status": status, "active": active}


class NodePoolTests(unittest.TestCase):
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
