import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

import control_api


class FakeManager:
    def __init__(self):
        self.created = []
        self.deleted = []

    def safe_candidate_snapshot(self):
        return [
            {
                "id": "node-safe",
                "country_short": "JP",
                "country": "Japan",
                "ip": "198.51.100.10",
                "proxy_type": "datacenter",
                "probe_status": "available",
            }
        ]

    def create_managed_slot(self, country, proxy_type):
        self.created.append((country, proxy_type))
        return {"slot": 2, "country": country, "proxy_type": proxy_type, "port": 17930, "status": "up"}

    def managed_slot_snapshot(self, slot):
        if slot != 2:
            return {"ok": False, "error_code": "slot_not_found"}
        return {"ok": True, "slot": 2, "country": "JP", "proxy_type": "datacenter", "port": 17930, "status": "up"}

    def rotate_managed_slot(self, slot):
        return {"ok": True, "slot": slot, "country": "JP", "proxy_type": "datacenter", "port": 17930, "status": "up"}

    def check_managed_slot(self, slot):
        return {"ok": True, "slot": slot, "exit_ip": "203.0.113.8", "egress_ok": True}

    def delete_managed_slot(self, slot):
        self.deleted.append(slot)
        return {"ok": True, "slot": slot}


class ControlAPITests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeManager()
        self.server = control_api.create_server(self.manager, ("127.0.0.1", 0), "test-control-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, payload=None, authorized=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Accept": "application/json"}
        body = None
        if authorized:
            headers["Authorization"] = "Bearer test-control-token"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        payload = json.loads(response_body) if response_body else None
        return response.status, response.getheaders(), payload

    def test_missing_bearer_token_is_rejected_without_service_data(self):
        status, _, payload = self.request("GET", "/control/v1/capabilities", authorized=False)
        self.assertEqual(status, 401)
        self.assertEqual(payload, {"error": {"code": "unauthorized"}})

    def test_capabilities_use_a_versioned_closed_contract(self):
        status, _, payload = self.request("GET", "/control/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["apiVersion"], "v1")
        self.assertEqual(
            payload["data"]["capabilities"],
            ["candidates.read", "slots.create", "slots.read", "slots.rotate", "slots.check", "slots.delete"],
        )

    def test_candidate_response_contains_only_manager_safe_snapshot(self):
        status, _, payload = self.request("GET", "/control/v1/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"][0]["id"], "node-safe")
        self.assertNotIn("config_text", json.dumps(payload))

    def test_create_slot_normalizes_country_and_preserves_closed_type(self):
        status, _, payload = self.request(
            "POST", "/control/v1/slots", {"country": "jp", "proxyType": "datacenter"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(self.manager.created, [("JP", "datacenter")])
        self.assertEqual(payload["data"]["slot"], 2)

    def test_create_slot_rejects_unknown_fields_before_calling_manager(self):
        status, _, payload = self.request(
            "POST", "/control/v1/slots", {"country": "JP", "proxyType": "datacenter", "config": "secret"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})
        self.assertEqual(self.manager.created, [])

    def test_slot_lifecycle_routes_return_stable_payloads(self):
        status, _, payload = self.request("GET", "/control/v1/slots/2")
        self.assertEqual((status, payload["data"]["port"]), (200, 17930))

        status, _, payload = self.request("POST", "/control/v1/slots/2/rotate", {})
        self.assertEqual((status, payload["data"]["slot"]), (200, 2))

        status, _, payload = self.request("POST", "/control/v1/slots/2/check", {})
        self.assertEqual((status, payload["data"]["egress_ok"]), (200, True))

        status, _, payload = self.request("DELETE", "/control/v1/slots/2")
        self.assertEqual((status, self.manager.deleted), (204, [2]))
        self.assertIsNone(payload)

    def test_start_control_server_reads_nonempty_token_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "control.token"
            token_file.write_text("file-token\n", encoding="utf-8")
            server = control_api.start_control_server(
                self.manager, "127.0.0.1:0", token_file, start=False
            )
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
