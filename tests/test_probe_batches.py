import unittest
from unittest import mock

import vpngate_manager as manager


class ProbeBatchTests(unittest.TestCase):
    def test_probe_nodes_returns_one_result_per_input_without_persisting(self):
        items = [
            {
                "id": "a",
                "config_text": "remote 127.0.0.1 443 tcp",
                "remote_host": "127.0.0.1",
                "remote_port": 443,
                "ping": 1,
            },
            {
                "id": "b",
                "config_text": "remote 127.0.0.2 443 tcp",
                "remote_host": "127.0.0.2",
                "remote_port": 443,
                "ping": 2,
            },
        ]

        def result(item):
            status = "available" if item["id"] == "a" else "unavailable"
            return dict(item, probe_status=status)

        with (
            mock.patch.object(manager, "tcp_prescreen_dead", return_value={}),
            mock.patch.object(manager.vpn_utils, "enrich_ip_info"),
            mock.patch.object(manager, "_probe_one_node", side_effect=result) as probe,
            mock.patch.object(manager, "write_json") as write,
        ):
            actual = manager.probe_nodes(items)

        self.assertEqual([item["id"] for item in actual], ["a", "b"])
        self.assertEqual(probe.call_count, 2)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
