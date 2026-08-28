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
        self.admin_updates = []
        self.country_refreshes = []
        self.refresh_start_result = {"state": "running", "country": "JP", "testedCount": 0}

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

    def safe_main_status(self):
        return {"country": "JP", "country_name": "Japan", "proxy_type": "datacenter", "exit_ip": "203.0.113.20", "port": 7928, "egress_ok": True, "active": True}

    def country_catalog_snapshot(self):
        return [
            {"code": "JP", "name": "日本", "candidateCount": 8, "observedAt": 1_700_000_000}
        ]

    def start_country_refresh(self, country):
        self.country_refreshes.append(country)
        return dict(self.refresh_start_result, country=country)

    def country_refresh_snapshot(self):
        return {
            "state": "completed",
            "country": "JP",
            "phase": "",
            "catalogCount": 20,
            "countryCandidateCount": 8,
            "testedCount": 5,
            "validCount": 4,
            "preservedCount": 1,
            "startedAt": 1_700_000_000,
            "finishedAt": 1_700_000_010,
            "errorCode": "",
        }

    def create_managed_slot(self, country, proxy_type, candidate_id=""):
        self.created.append((country, proxy_type, candidate_id))
        return {"slot": 2, "country": country, "proxy_type": proxy_type, "port": 17930, "status": "up"}

    def managed_slots_snapshot(self):
        return [self.managed_slot_snapshot(2)]

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

    def managed_account_status(self):
        return {"username": "owner", "totpSupported": False}

    def update_managed_account(self, username, password):
        self.admin_updates.append((username, password))
        return {"ok": True}

    def issue_managed_ui_session(self):
        return {
            "cookieName": "session",
            "sessionToken": "opaque-session-token",
            "expiresAt": 1_700_000_300,
        }


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
            [
                "candidates.read",
                "candidate-countries.read",
                "candidates.refresh.country",
                "candidates.refresh.status",
                "slots.create",
                "slots.read",
                "slots.rotate",
                "slots.check",
                "slots.delete",
                "main.read",
                "admin.read",
                "admin.verify",
                "admin.update",
                "admin.sessions.issue",
            ],
        )

    def test_main_egress_returns_only_safe_status(self):
        status, _, payload = self.request("GET", "/control/v1/main")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["port"], 7928)
        serialized = json.dumps(payload).lower()
        for forbidden in ["password", "token", "config", "process"]:
            self.assertNotIn(forbidden, serialized)

    def test_country_catalog_and_refresh_use_a_closed_safe_contract(self):
        status, _, payload = self.request("GET", "/control/v1/candidates/countries")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"][0]["code"], "JP")
        self.assertNotIn("config", json.dumps(payload).lower())

        status, _, payload = self.request(
            "POST", "/control/v1/candidates/refresh", {"country": "jp"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.manager.country_refreshes, ["JP"])
        self.assertEqual(payload["data"]["state"], "running")

        status, _, payload = self.request("GET", "/control/v1/candidates/refresh")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["testedCount"], 5)
        serialized = json.dumps(payload).lower()
        for forbidden in ["password", "token", "config", "exception"]:
            self.assertNotIn(forbidden, serialized)

    def test_country_refresh_rejects_unknown_fields_and_maps_busy(self):
        status, _, payload = self.request(
            "POST",
            "/control/v1/candidates/refresh",
            {"country": "JP", "config": "must-not-pass"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})
        self.assertEqual(self.manager.country_refreshes, [])

        self.manager.refresh_start_result = {
            "state": "failed",
            "country": "JP",
            "errorCode": "maintenance_busy",
        }
        status, _, payload = self.request(
            "POST", "/control/v1/candidates/refresh", {"country": "JP"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": {"code": "maintenance_busy"}})

    def test_candidate_response_contains_only_manager_safe_snapshot(self):
        status, _, payload = self.request("GET", "/control/v1/candidates")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"][0]["id"], "node-safe")
        self.assertNotIn("config_text", json.dumps(payload))

    def test_create_slot_normalizes_country_and_preserves_closed_type(self):
        status, _, payload = self.request(
            "POST",
            "/control/v1/slots",
            {"country": "jp", "proxyType": "datacenter", "candidateId": "node-safe"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(self.manager.created, [("JP", "datacenter", "node-safe")])
        self.assertEqual(payload["data"]["slot"], 2)

    def test_slots_collection_returns_only_manager_safe_snapshots(self):
        status, _, payload = self.request("GET", "/control/v1/slots")

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"][0]["slot"], 2)
        self.assertNotIn("process", json.dumps(payload))

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

    def test_admin_contract_updates_credentials_without_returning_secrets(self):
        status, _, payload = self.request("GET", "/control/v1/admin")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"data": {"username": "owner", "totpSupported": False}})

        status, _, payload = self.request(
            "PUT",
            "/control/v1/admin",
            {"username": "renamed", "password": "new-password-marker"},
        )
        self.assertEqual(status, 204)
        self.assertIsNone(payload)
        self.assertEqual(self.manager.admin_updates, [("renamed", "new-password-marker")])

    def test_admin_update_rejects_unknown_fields_before_calling_manager(self):
        status, _, payload = self.request(
            "PUT",
            "/control/v1/admin",
            {"username": "renamed", "password": "new-password-marker", "secret_path": "forbidden"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})
        self.assertEqual(self.manager.admin_updates, [])

    def test_admin_verify_returns_no_account_material(self):
        self.manager.verify_managed_account = lambda username, password: {
            "ok": username == "owner" and password == "old-password-marker",
            **({} if username == "owner" and password == "old-password-marker" else {"error_code": "credentials_rejected"}),
        }
        status, _, payload = self.request(
            "POST",
            "/control/v1/admin/verify",
            {"username": "owner", "password": "old-password-marker"},
        )
        self.assertEqual(status, 204)
        self.assertIsNone(payload)

    def test_admin_session_contract_returns_only_opaque_cookie_material(self):
        status, headers, payload = self.request("POST", "/control/v1/admin/sessions", {})
        self.assertEqual(status, 201)
        self.assertEqual(
            payload,
            {
                "data": {
                    "cookieName": "session",
                    "sessionToken": "opaque-session-token",
                    "expiresAt": 1_700_000_300,
                }
            },
        )
        serialized = json.dumps(payload)
        self.assertNotIn("password", serialized)
        self.assertNotIn("secret_path", serialized)
        self.assertNotIn("Set-Cookie", dict(headers))


if __name__ == "__main__":
    unittest.main()
