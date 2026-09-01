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
        self.main_assignments = []
        self.main_commits = []
        self.main_rollbacks = []
        self.main_repair_commits = []
        self.main_repair_replacements = []
        self.mutation_leases = []
        self.main_assignment_result = {
            "ok": True,
            "operation_id": "operation-safe-1",
            "state": "pending_commit",
            "old_candidate_id": "old-main",
            "new_candidate_id": "node-safe",
            "country": "JP",
            "proxy_type": "datacenter",
            "port": 7928,
            "dns_verified": True,
            "exit_verified": True,
            "available": True,
            "expires_at": 1_700_000_180,
        }
        self.refresh_start_result = {"state": "running", "country": "JP", "testedCount": 0}

    def safe_candidate_snapshot(self):
        return [
            {
                "id": "node-safe",
                "country_short": "JP",
                "country": "Japan",
                "ip": "198.51.100.10",
                "exit_ip": "203.0.113.10",
                "exit_ip_checked_at": 1_700_000_005,
                "proxy_type": "datacenter",
                "probe_status": "available",
            }
        ]

    def safe_main_status(self):
        return {"candidate_id": "old-main", "country": "JP", "country_name": "Japan", "proxy_type": "datacenter", "exit_ip": "203.0.113.20", "port": 7928, "egress_ok": True, "active": True}

    def main_assignment_snapshot(self):
        return dict(self.main_assignment_result)

    def stage_main_assignment(self, candidate_id, country, proxy_type, expected, idempotency_key):
        self.main_assignments.append((candidate_id, country, proxy_type, expected, idempotency_key))
        return dict(self.main_assignment_result)

    def commit_main_assignment(self, operation_id):
        self.main_commits.append(operation_id)
        return dict(self.main_assignment_result, state="committed")

    def rollback_main_assignment(self, operation_id):
        self.main_rollbacks.append(operation_id)
        return dict(self.main_assignment_result, state="rolled_back")

    def repair_commit_main_assignment(self, operation_id):
        self.main_repair_commits.append(operation_id)
        return dict(
            self.main_assignment_result,
            state="pending_gateway_validation",
            resolution="repair_commit",
        )

    def repair_replace_main_assignment(self, operation_id, candidate_id, country, proxy_type):
        self.main_repair_replacements.append(
            (operation_id, candidate_id, country, proxy_type)
        )
        return dict(
            self.main_assignment_result,
            state="pending_gateway_validation",
            new_candidate_id=candidate_id,
            resolution="repair_replace",
        )

    def acquire_mutation_lease(self, idempotency_key):
        self.mutation_leases.append(("acquire", idempotency_key))
        return {"ok": True, "state": "active", "lease_id": "opaque-lease-id-00000000000000000001", "expires_at": 1_700_000_060}

    def renew_mutation_lease(self, lease_id):
        self.mutation_leases.append(("renew", lease_id))
        return {"ok": True, "state": "active", "lease_id": lease_id, "expires_at": 1_700_000_120}

    def release_mutation_lease(self, lease_id):
        self.mutation_leases.append(("release", lease_id))
        return {"ok": True, "state": "released"}

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

    def assign_managed_slot(self, slot, candidate_id, country, proxy_type):
        return {"ok": True, "slot": slot, "candidate_id": candidate_id, "country": country, "proxy_type": proxy_type, "port": 17930, "status": "up"}

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
                "slots.assign",
                "slots.delete",
                "main.read",
                "main.assignment.read",
                "main.assign",
                "main.assign.commit",
                "main.assign.rollback",
                "main.assign.repair-commit",
                "main.assign.repair-replace",
                "mutation-leases.acquire",
                "mutation-leases.renew",
                "mutation-leases.release",
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

    def test_main_assignment_uses_closed_routes_and_safe_payloads(self):
        status, _, payload = self.request("GET", "/control/v1/main/assignment")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "pending_commit")

        request = {
            "candidateId": "node-safe",
            "country": "jp",
            "proxyType": "datacenter",
            "expectedCurrentCandidateId": "old-main",
            "idempotencyKey": "gateway-operation-1",
        }
        status, _, payload = self.request("POST", "/control/v1/main/assign", request)
        self.assertEqual(status, 202)
        self.assertEqual(
            self.manager.main_assignments,
            [("node-safe", "JP", "datacenter", "old-main", "gateway-operation-1")],
        )
        self.assertEqual(payload["data"]["operation_id"], "operation-safe-1")

        status, _, payload = self.request(
            "POST", "/control/v1/main/assign/operation-safe-1/commit", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "committed")

        status, _, payload = self.request(
            "POST", "/control/v1/main/assign/operation-safe-1/rollback", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "rolled_back")
        serialized = json.dumps(payload).lower()
        for forbidden in ["password", "token", "config", "cookie", "private"]:
            self.assertNotIn(forbidden, serialized)

        status, _, payload = self.request(
            "POST", "/control/v1/main/assign/operation-safe-1/repair-commit", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "pending_gateway_validation")
        self.assertEqual(payload["data"]["resolution"], "repair_commit")
        self.assertEqual(self.manager.main_repair_commits, ["operation-safe-1"])

        status, _, payload = self.request(
            "POST",
            "/control/v1/main/assign/operation-safe-1/repair-replace",
            {"candidateId": "replacement-safe", "country": "kr", "proxyType": "residential"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "pending_gateway_validation")
        self.assertEqual(payload["data"]["resolution"], "repair_replace")
        self.assertEqual(
            self.manager.main_repair_replacements,
            [("operation-safe-1", "replacement-safe", "KR", "residential")],
        )

    def test_main_assignment_rejects_unknown_fields_and_maps_busy(self):
        request = {
            "candidateId": "node-safe",
            "country": "JP",
            "proxyType": "datacenter",
            "expectedCurrentCandidateId": "old-main",
            "idempotencyKey": "gateway-operation-1",
            "config": "must-not-pass",
        }
        status, _, payload = self.request("POST", "/control/v1/main/assign", request)
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})
        self.assertEqual(self.manager.main_assignments, [])

        self.manager.main_assignment_result = {"ok": False, "error_code": "operation_busy"}
        request.pop("config")
        status, _, payload = self.request("POST", "/control/v1/main/assign", request)
        self.assertEqual(status, 409)
        self.assertEqual(payload, {"error": {"code": "operation_busy"}})

        status, _, payload = self.request(
            "POST", "/control/v1/main/assign/not.valid/commit", {}
        )
        self.assertEqual(status, 404)

    def test_main_assignment_read_exposes_repair_state_as_data(self):
        self.manager.main_assignment_result = {
            "ok": False,
            "operation_id": "operation-safe-1",
            "state": "repair_required",
            "old_candidate_id": "old-main",
            "new_candidate_id": "node-safe",
            "port": 7928,
            "dns_verified": False,
            "exit_verified": False,
            "available": False,
            "error_code": "rollback_failed",
            "expires_at": 1_700_000_180,
        }

        status, _, payload = self.request("GET", "/control/v1/main/assignment")

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "repair_required")
        self.assertNotIn("ok", payload["data"])

    def test_mutation_lease_uses_closed_acquire_renew_release_routes(self):
        status, _, acquired = self.request(
            "POST",
            "/control/v1/mutation-leases",
            {"idempotencyKey": "gateway-protocol-operation"},
        )
        self.assertEqual(status, 201)
        lease_id = acquired["data"]["lease_id"]
        self.assertEqual(acquired["data"]["state"], "active")

        status, _, renewed = self.request(
            "POST", f"/control/v1/mutation-leases/{lease_id}/renew", {}
        )
        self.assertEqual(status, 200)
        self.assertGreater(renewed["data"]["expires_at"], acquired["data"]["expires_at"])

        status, _, released = self.request(
            "DELETE", f"/control/v1/mutation-leases/{lease_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(released, {"data": {"state": "released"}})
        self.assertEqual(
            self.manager.mutation_leases,
            [
                ("acquire", "gateway-protocol-operation"),
                ("renew", lease_id),
                ("release", lease_id),
            ],
        )

        status, _, payload = self.request(
            "POST",
            "/control/v1/mutation-leases",
            {"idempotencyKey": "gateway-protocol-operation", "ttl": 999},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})

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
        self.assertEqual(payload["data"][0]["ip"], "198.51.100.10")
        self.assertEqual(payload["data"][0]["exit_ip"], "203.0.113.10")
        self.assertEqual(payload["data"][0]["exit_ip_checked_at"], 1_700_000_005)
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

    def test_assign_slot_route_uses_closed_payload(self):
        status, _, payload = self.request(
            "POST", "/control/v1/slots/2/assign",
            {"candidateId": "node-safe", "country": "jp", "proxyType": "datacenter"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["candidate_id"], "node-safe")

        status, _, payload = self.request(
            "POST", "/control/v1/slots/2/assign",
            {"candidateId": "node-safe", "country": "JP", "proxyType": "datacenter", "config": "forbidden"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error": {"code": "invalid_request"}})

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
