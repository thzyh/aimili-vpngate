import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from main_assignment import MainAssignmentCoordinator
import vpngate_manager as manager


class FakeExecutor:
    def __init__(self):
        self.calls = []
        self.stage_result = {
            "dns_verified": True,
            "exit_verified": True,
            "available": True,
        }
        self.restore_result = {
            "dns_verified": True,
            "exit_verified": True,
            "available": True,
        }

    def stage(self, candidate_id):
        self.calls.append(("stage", candidate_id))
        return dict(self.stage_result)

    def restore(self, previous):
        self.calls.append(("restore", previous["candidate_id"]))
        return dict(self.restore_result)


def current_snapshot(candidate_id="old-main"):
    return {
        "candidate_id": candidate_id,
        "country": "US",
        "proxy_type": "datacenter",
        "connection_enabled": True,
        "routing_mode": "fixed_ip",
        "routing_ip_type": "all",
        "fixed_node_id": candidate_id,
        "restorable": True,
        "config_text": "must-not-be-persisted",
    }


class MainAssignmentCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "main-assignment.json"
        self.clock = [1_000.0]
        self.executor = FakeExecutor()
        self.coordinator = MainAssignmentCoordinator(
            self.path,
            now=lambda: self.clock[0],
            operation_id_factory=lambda: "op-safe-1",
        )

    def tearDown(self):
        self.temp.cleanup()

    def stage(self, **overrides):
        values = {
            "candidate_id": "new-main",
            "country": "JP",
            "proxy_type": "datacenter",
            "expected_current_candidate_id": "old-main",
            "idempotency_key": "gateway-operation-1",
            "current": current_snapshot(),
            "slot_candidate_ids": {"slot-one", "slot-two"},
            "stage_candidate": self.executor.stage,
            "restore_previous": self.executor.restore,
        }
        values.update(overrides)
        return self.coordinator.stage(**values)

    def legacy_terminal_document(self) -> dict:
        path = Path(self.temp.name) / "legacy-terminal-source.json"
        operation_counter = iter(range(1, 8))
        coordinator = MainAssignmentCoordinator(
            path,
            now=lambda: self.clock[0],
            operation_id_factory=lambda: f"legacy-terminal-op-{next(operation_counter)}",
        )
        for index in range(7):
            result = coordinator.stage(
                candidate_id=f"candidate-{index}",
                country="JP",
                proxy_type="datacenter",
                expected_current_candidate_id="old-main",
                idempotency_key=f"legacy-terminal-key-{index}",
                current=current_snapshot(),
                slot_candidate_ids=set(),
                stage_candidate=lambda _candidate: {
                    "dns_verified": True,
                    "exit_verified": True,
                    "available": True,
                },
                restore_previous=self.executor.restore,
            )
            if index < 5:
                coordinator.commit(result["operation_id"])
            else:
                coordinator.rollback(result["operation_id"], self.executor.restore)
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("mutation_lease")
        return document

    def test_successful_stage_is_pending_until_commit(self):
        result = self.stage()

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "pending_commit")
        self.assertEqual(result["operation_id"], "op-safe-1")
        self.assertEqual(result["old_candidate_id"], "old-main")
        self.assertEqual(result["new_candidate_id"], "new-main")
        self.assertEqual(result["port"], 7928)
        self.assertTrue(result["dns_verified"])
        self.assertTrue(result["exit_verified"])
        self.assertTrue(result["available"])
        self.assertEqual(self.executor.calls, [("stage", "new-main")])
        self.assertFalse(self.coordinator.mutation_allowed())
        self.assertEqual(
            self.coordinator.reserved_candidate_ids(),
            {"old-main", "new-main"},
        )

        committed = self.coordinator.commit("op-safe-1")

        self.assertEqual(committed["state"], "committed")
        self.assertTrue(self.coordinator.mutation_allowed())

    def test_idempotency_replays_same_request_and_rejects_conflict(self):
        first = self.stage()
        replay = self.stage()
        conflict = self.stage(candidate_id="different-main")

        self.assertEqual(replay, first)
        self.assertEqual(self.executor.calls, [("stage", "new-main")])
        self.assertEqual(conflict, {"ok": False, "error_code": "idempotency_conflict"})

    def test_rejects_current_mismatch_and_candidates_used_by_slots(self):
        mismatch = self.stage(expected_current_candidate_id="stale-main")
        in_use = self.stage(
            candidate_id="slot-two",
            idempotency_key="gateway-operation-2",
        )

        self.assertEqual(mismatch, {"ok": False, "error_code": "current_mismatch"})
        self.assertEqual(in_use, {"ok": False, "error_code": "candidate_in_use"})
        self.assertEqual(self.executor.calls, [])

    def test_rejects_stage_when_current_main_has_no_restore_material(self):
        current = current_snapshot()
        current["restorable"] = False

        result = self.stage(current=current)

        self.assertEqual(result, {"ok": False, "error_code": "current_not_restorable"})
        self.assertEqual(self.executor.calls, [])

    def test_failed_candidate_validation_restores_previous_main(self):
        for failed_field in ("dns_verified", "exit_verified", "available"):
            with self.subTest(failed_field=failed_field):
                self.executor.calls.clear()
                self.executor.stage_result = {
                    "dns_verified": True,
                    "exit_verified": True,
                    "available": True,
                }
                self.executor.stage_result[failed_field] = False
                result = self.stage(idempotency_key=f"failure-{failed_field}")

                self.assertEqual(result["state"], "rolled_back")
                self.assertEqual(result["error_code"], "assign_failed_rolled_back")
                self.assertEqual(
                    self.executor.calls,
                    [("stage", "new-main"), ("restore", "old-main")],
                )
                self.assertTrue(self.coordinator.mutation_allowed())

    def test_restore_failure_enters_repair_required(self):
        self.executor.stage_result["available"] = False
        self.executor.restore_result["exit_verified"] = False

        result = self.stage()

        self.assertEqual(result["state"], "repair_required")
        self.assertEqual(result["error_code"], "rollback_failed")
        self.assertFalse(self.coordinator.mutation_allowed())

    def test_explicit_rollback_retries_a_repair_required_restore(self):
        self.executor.stage_result["available"] = False
        self.executor.restore_result["exit_verified"] = False
        failed = self.stage()
        self.executor.restore_result["exit_verified"] = True

        retried = self.coordinator.rollback("op-safe-1", self.executor.restore)

        self.assertEqual("repair_required", failed["state"])
        self.assertEqual("rolled_back", retried["state"])
        self.assertEqual("", retried["error_code"])
        self.assertTrue(self.coordinator.mutation_allowed())

    def test_repair_commit_waits_for_gateway_validation_and_replays_without_reconnect(self):
        self.executor.stage_result["available"] = False
        self.executor.restore_result["exit_verified"] = False
        failed = self.stage()
        self.executor.stage_result["available"] = True

        repaired = self.coordinator.repair_commit("op-safe-1", self.executor.stage)
        replay = self.coordinator.repair_commit("op-safe-1", self.executor.stage)

        self.assertEqual("repair_required", failed["state"])
        self.assertEqual("pending_gateway_validation", repaired["state"])
        self.assertEqual("repair_commit", repaired["resolution"])
        self.assertTrue(repaired["ok"])
        self.assertEqual(repaired, replay)
        self.assertEqual(self.executor.calls.count(("stage", "new-main")), 2)
        self.assertFalse(self.coordinator.mutation_allowed())

        committed = self.coordinator.commit("op-safe-1")
        duplicate_finalize = self.coordinator.commit("op-safe-1")

        self.assertEqual("committed", committed["state"])
        self.assertEqual(committed, duplicate_finalize)
        self.assertTrue(self.coordinator.mutation_allowed())

    def test_repair_replace_waits_for_gateway_validation_and_rejects_a_different_replay(self):
        self.executor.stage_result["available"] = False
        self.executor.restore_result["exit_verified"] = False
        failed = self.stage()
        self.executor.stage_result["available"] = True

        repaired = self.coordinator.repair_replace(
            "op-safe-1",
            "replacement-main",
            "KR",
            "residential",
            self.executor.stage,
        )

        self.assertEqual("repair_required", failed["state"])
        replay = self.coordinator.repair_replace(
            "op-safe-1",
            "replacement-main",
            "KR",
            "residential",
            self.executor.stage,
        )
        conflict = self.coordinator.repair_replace(
            "op-safe-1",
            "different-main",
            "DE",
            "datacenter",
            self.executor.stage,
        )

        self.assertEqual("pending_gateway_validation", repaired["state"])
        self.assertEqual("replacement-main", repaired["new_candidate_id"])
        self.assertEqual("repair_replace", repaired["resolution"])
        self.assertTrue(repaired["ok"])
        self.assertEqual(repaired, replay)
        self.assertEqual("idempotency_conflict", conflict["error_code"])
        self.assertEqual(self.executor.calls.count(("stage", "replacement-main")), 1)
        self.assertFalse(self.coordinator.mutation_allowed())

    def test_repair_pending_gateway_validation_survives_restart_and_response_loss(self):
        self.executor.stage_result["available"] = False
        self.executor.restore_result["exit_verified"] = False
        self.stage()
        self.executor.stage_result["available"] = True
        first = self.coordinator.repair_commit("op-safe-1", self.executor.stage)
        restarted = MainAssignmentCoordinator(self.path, now=lambda: self.clock[0])

        replay = restarted.repair_commit("op-safe-1", self.executor.stage)

        self.assertEqual("pending_gateway_validation", first["state"])
        self.assertEqual(first, replay)
        self.assertEqual(self.executor.calls.count(("stage", "new-main")), 2)
        self.assertFalse(restarted.mutation_allowed())

    def test_expired_repair_cannot_replay_or_finalize_before_recovery(self):
        self.executor.stage_result["available"] = False
        self.executor.restore_result["exit_verified"] = False
        self.stage()
        self.executor.stage_result["available"] = True
        self.coordinator.repair_commit("op-safe-1", self.executor.stage)
        self.clock[0] += 181

        replay = self.coordinator.repair_commit("op-safe-1", self.executor.stage)
        finalized = self.coordinator.commit("op-safe-1")

        self.assertEqual("operation_expired", replay["error_code"])
        self.assertEqual("operation_expired", finalized["error_code"])
        self.assertEqual(self.executor.calls.count(("stage", "new-main")), 2)
        self.assertFalse(self.coordinator.mutation_allowed())

    def test_expired_pending_operation_is_rolled_back_after_restart(self):
        self.stage()
        self.clock[0] += 181
        restarted = MainAssignmentCoordinator(self.path, now=lambda: self.clock[0])

        result = restarted.recover(self.executor.restore)

        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(result["error_code"], "assignment_expired")
        self.assertEqual(self.executor.calls[-1], ("restore", "old-main"))
        self.assertTrue(restarted.mutation_allowed())

    def test_recovery_does_not_restore_an_unexpired_stage_owned_by_this_process(self):
        entered = threading.Event()
        release = threading.Event()
        staged = []

        def blocking_stage(candidate_id):
            entered.set()
            release.wait(1)
            return self.executor.stage(candidate_id)

        worker = threading.Thread(
            target=lambda: staged.append(self.stage(stage_candidate=blocking_stage))
        )
        worker.start()
        self.assertTrue(entered.wait(1))

        recovered = self.coordinator.recover(self.executor.restore)
        release.set()
        worker.join(1)

        self.assertEqual(recovered["state"], "switching")
        self.assertEqual(staged[0]["state"], "pending_commit")
        self.assertNotIn(("restore", "old-main"), self.executor.calls)

    def test_expired_recovery_wins_over_a_stale_stage_callback(self):
        entered = threading.Event()
        release = threading.Event()
        staged = []

        def blocking_stage(candidate_id):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.executor.stage(candidate_id)

        worker = threading.Thread(
            target=lambda: staged.append(self.stage(stage_candidate=blocking_stage))
        )
        worker.start()
        self.assertTrue(entered.wait(1))
        self.clock[0] += 181

        recovered = self.coordinator.recover(self.executor.restore)
        release.set()
        worker.join(2)

        self.assertEqual(recovered["state"], "rolled_back")
        self.assertEqual(
            staged[0],
            {"ok": False, "error_code": "operation_state_conflict"},
        )
        self.assertEqual(
            self.coordinator.commit("op-safe-1"),
            {"ok": False, "error_code": "operation_state_conflict"},
        )
        self.assertEqual(self.executor.calls.count(("restore", "old-main")), 1)

    def test_expired_recovery_wins_over_a_stale_repair_callback(self):
        self.executor.stage_result["available"] = False
        self.executor.restore_result["available"] = False
        self.assertEqual(self.stage()["state"], "repair_required")
        entered = threading.Event()
        release = threading.Event()
        repaired = []

        def blocking_repair(candidate_id):
            entered.set()
            self.assertTrue(release.wait(2))
            return {
                "dns_verified": True,
                "exit_verified": True,
                "available": True,
            }

        worker = threading.Thread(
            target=lambda: repaired.append(
                self.coordinator.repair_commit("op-safe-1", blocking_repair)
            )
        )
        worker.start()
        self.assertTrue(entered.wait(1))
        self.clock[0] += 181
        self.executor.restore_result = {
            "dns_verified": True,
            "exit_verified": True,
            "available": True,
        }

        recovered = self.coordinator.recover(self.executor.restore)
        release.set()
        worker.join(2)

        self.assertEqual(recovered["state"], "rolled_back")
        self.assertEqual(
            repaired[0],
            {"ok": False, "error_code": "operation_state_conflict"},
        )
        self.assertEqual(
            self.coordinator.commit("op-safe-1"),
            {"ok": False, "error_code": "operation_state_conflict"},
        )

    def test_existing_corrupt_state_is_fail_closed_and_observable(self):
        self.path.write_text("{broken", encoding="utf-8")

        coordinator = MainAssignmentCoordinator(self.path)

        self.assertEqual(
            coordinator.snapshot(),
            {"ok": False, "state": "repair_required", "error_code": "state_corrupt"},
        )
        self.assertFalse(coordinator.mutation_allowed())
        self.assertEqual(
            coordinator.stage(
                candidate_id="new-main",
                country="JP",
                proxy_type="datacenter",
                expected_current_candidate_id="old-main",
                idempotency_key="gateway-operation-corrupt",
                current=current_snapshot(),
                slot_candidate_ids=set(),
                stage_candidate=self.executor.stage,
                restore_previous=self.executor.restore,
            ),
            {"ok": False, "error_code": "operation_busy"},
        )
        self.assertEqual(self.executor.calls, [])

    def test_corrupt_state_recovery_stays_fail_closed_without_restoring(self):
        self.path.write_text("{broken", encoding="utf-8")
        coordinator = MainAssignmentCoordinator(self.path)

        recovered = coordinator.recover(self.executor.restore)

        self.assertEqual(
            recovered,
            {"ok": False, "state": "repair_required", "error_code": "state_corrupt"},
        )
        self.assertEqual(self.executor.calls, [])

    def test_valid_json_with_invalid_state_structure_is_fail_closed(self):
        self.stage()
        valid_operation_state = json.loads(self.path.read_text(encoding="utf-8"))
        lease_path = Path(self.temp.name) / "lease-state.json"
        lease_coordinator = MainAssignmentCoordinator(
            lease_path,
            now=lambda: self.clock[0],
        )
        lease_coordinator.acquire_mutation_lease("gateway-lease-schema")
        valid_lease_state = json.loads(lease_path.read_text(encoding="utf-8"))

        invalid_documents = []
        active_empty = json.loads(json.dumps(valid_operation_state))
        active_empty["active"] = {}
        invalid_documents.append(active_empty)

        history_empty = json.loads(json.dumps(valid_operation_state))
        operation_key = next(iter(history_empty["operations"]))
        history_empty["operations"][operation_key] = {}
        invalid_documents.append(history_empty)

        invalid_enum = json.loads(json.dumps(valid_operation_state))
        operation_key = next(iter(invalid_enum["operations"]))
        invalid_enum["active"]["state"] = "unexpected_state"
        invalid_enum["operations"][operation_key]["state"] = "unexpected_state"
        invalid_documents.append(invalid_enum)

        invalid_enum_type = json.loads(json.dumps(valid_operation_state))
        operation_key = next(iter(invalid_enum_type["operations"]))
        invalid_enum_type["active"]["state"] = []
        invalid_enum_type["operations"][operation_key]["state"] = []
        invalid_documents.append(invalid_enum_type)

        invalid_scalar = json.loads(json.dumps(valid_operation_state))
        operation_key = next(iter(invalid_scalar["operations"]))
        invalid_scalar["active"]["operation_id"] = 123
        invalid_scalar["operations"][operation_key]["operation_id"] = 123
        invalid_documents.append(invalid_scalar)

        invalid_lease = json.loads(json.dumps(valid_lease_state))
        invalid_lease["mutation_lease"]["state"] = "unexpected_state"
        invalid_lease["mutation_lease"]["private_body"] = "must-not-be-exposed"
        invalid_documents.append(invalid_lease)

        invalid_lease_enum_type = json.loads(json.dumps(valid_lease_state))
        invalid_lease_enum_type["mutation_lease"]["state"] = []
        invalid_documents.append(invalid_lease_enum_type)

        for payload in invalid_documents:
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload), encoding="utf-8")
                coordinator = MainAssignmentCoordinator(self.path)

                snapshot = coordinator.snapshot()

                self.assertEqual(
                    snapshot,
                    {
                        "ok": False,
                        "state": "repair_required",
                        "error_code": "state_corrupt",
                    },
                )
                self.assertFalse(coordinator.mutation_allowed())
                self.assertNotIn("must-not-be-exposed", json.dumps(snapshot))

    def test_legacy_v1_idle_state_is_atomically_migrated(self):
        self.path.write_text(
            json.dumps({"version": 1, "active": None, "operations": {}}),
            encoding="utf-8",
        )

        coordinator = MainAssignmentCoordinator(self.path)

        self.assertEqual(coordinator.snapshot(), {"ok": True, "state": "idle"})
        self.assertTrue(coordinator.mutation_allowed())
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            stored,
            {
                "version": 1,
                "active": None,
                "operations": {},
                "mutation_lease": None,
            },
        )
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_legacy_v1_pending_and_repair_states_are_migrated(self):
        self.stage()
        pending = json.loads(self.path.read_text(encoding="utf-8"))
        pending.pop("mutation_lease")

        repair_path = Path(self.temp.name) / "legacy-repair.json"
        repair_coordinator = MainAssignmentCoordinator(
            repair_path,
            now=lambda: self.clock[0],
            operation_id_factory=lambda: "repair-op-safe-1",
        )
        repair = repair_coordinator.stage(
            candidate_id="new-main",
            country="JP",
            proxy_type="datacenter",
            expected_current_candidate_id="old-main",
            idempotency_key="legacy-repair-operation",
            current=current_snapshot(),
            slot_candidate_ids=set(),
            stage_candidate=lambda _candidate: {},
            restore_previous=lambda _previous: {},
        )
        self.assertEqual(repair["state"], "repair_required")
        repair_document = json.loads(repair_path.read_text(encoding="utf-8"))
        repair_document.pop("mutation_lease")

        for name, payload, expected_state in (
            ("pending", pending, "pending_commit"),
            ("repair", repair_document, "repair_required"),
        ):
            with self.subTest(name=name):
                path = Path(self.temp.name) / f"legacy-{name}-restart.json"
                path.write_text(json.dumps(payload), encoding="utf-8")

                restarted = MainAssignmentCoordinator(
                    path,
                    now=lambda: self.clock[0],
                )

                self.assertEqual(restarted.snapshot()["state"], expected_state)
                self.assertFalse(restarted.mutation_allowed())
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("mutation_lease", stored)
                self.assertIsNone(stored["mutation_lease"])
                self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_explicit_invalid_mutation_lease_is_not_migrated(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "active": None,
                    "operations": {},
                    "mutation_lease": {},
                }
            ),
            encoding="utf-8",
        )

        coordinator = MainAssignmentCoordinator(self.path)

        self.assertEqual(
            coordinator.snapshot(),
            {"ok": False, "state": "repair_required", "error_code": "state_corrupt"},
        )
        self.assertFalse(coordinator.mutation_allowed())
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["mutation_lease"], {})

    def test_legacy_terminal_repair_resolutions_are_canonically_migrated(self):
        legacy = self.legacy_terminal_document()
        committed = [
            operation
            for operation in legacy["operations"].values()
            if operation["state"] == "committed"
        ]
        committed[0]["resolution"] = "committed_new_after_repair"
        committed[1]["resolution"] = "replaced_after_repair"
        self.path.write_text(json.dumps(legacy), encoding="utf-8")

        coordinator = MainAssignmentCoordinator(self.path)

        self.assertEqual(coordinator.snapshot(), {"ok": True, "state": "idle"})
        self.assertTrue(coordinator.mutation_allowed())
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsNone(stored["mutation_lease"])
        self.assertEqual(len(stored["operations"]), 7)
        self.assertEqual(
            {operation["state"] for operation in stored["operations"].values()},
            {"committed", "rolled_back"},
        )
        self.assertFalse(
            any("resolution" in operation for operation in stored["operations"].values())
        )
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_legacy_resolution_compatibility_rejects_nonterminal_or_invalid_records(self):
        cases = {}

        active = self.legacy_terminal_document()
        active_operation = next(iter(active["operations"].values()))
        active_operation["resolution"] = "committed_new_after_repair"
        active["active"] = dict(active_operation)
        cases["active"] = active

        pending = self.legacy_terminal_document()
        pending_operation = next(iter(pending["operations"].values()))
        pending_operation["state"] = "pending_commit"
        pending_operation["resolution"] = "committed_new_after_repair"
        pending["active"] = dict(pending_operation)
        cases["pending"] = pending

        repairing = self.legacy_terminal_document()
        repairing_operation = next(iter(repairing["operations"].values()))
        repairing_operation["state"] = "repairing"
        repairing_operation["resolution"] = "replaced_after_repair"
        repairing["active"] = dict(repairing_operation)
        cases["repairing"] = repairing

        rolled_back = self.legacy_terminal_document()
        rolled_back_operation = next(
            operation
            for operation in rolled_back["operations"].values()
            if operation["state"] == "rolled_back"
        )
        rolled_back_operation["resolution"] = "committed_new_after_repair"
        cases["rolled_back"] = rolled_back

        unknown = self.legacy_terminal_document()
        next(iter(unknown["operations"].values()))["resolution"] = "unknown_resolution"
        cases["unknown"] = unknown

        bad_hash = self.legacy_terminal_document()
        bad_hash_operation = next(iter(bad_hash["operations"].values()))
        bad_hash_operation["resolution"] = "committed_new_after_repair"
        bad_hash_operation["repair_request_hash"] = "not-a-valid-hash"
        cases["bad_hash"] = bad_hash

        for name, payload in cases.items():
            with self.subTest(name=name):
                path = Path(self.temp.name) / f"legacy-terminal-invalid-{name}.json"
                original = json.dumps(payload, sort_keys=True)
                path.write_text(original, encoding="utf-8")

                coordinator = MainAssignmentCoordinator(path)

                self.assertEqual(
                    coordinator.snapshot(),
                    {
                        "ok": False,
                        "state": "repair_required",
                        "error_code": "state_corrupt",
                    },
                )
                self.assertFalse(coordinator.mutation_allowed())
                self.assertEqual(
                    json.dumps(json.loads(path.read_text(encoding="utf-8")), sort_keys=True),
                    original,
                )

    def test_external_mutation_lease_is_finite_idempotent_and_fail_closed(self):
        acquire = getattr(self.coordinator, "acquire_mutation_lease", None)
        self.assertIsNotNone(acquire)

        first = acquire("gateway-protocol-operation")
        replay = acquire("gateway-protocol-operation")
        conflict = acquire("different-protocol-operation")

        self.assertTrue(first["ok"])
        self.assertEqual(first, replay)
        self.assertEqual(first["state"], "active")
        self.assertGreaterEqual(len(first["lease_id"]), 32)
        self.assertEqual(conflict, {"ok": False, "error_code": "lease_busy"})
        self.assertFalse(self.coordinator.mutation_allowed())

        self.clock[0] += 30
        renewed = self.coordinator.renew_mutation_lease(first["lease_id"])
        self.assertGreater(renewed["expires_at"], first["expires_at"])
        self.assertEqual(
            self.coordinator.renew_mutation_lease("wrong-opaque-lease-id"),
            {"ok": False, "error_code": "lease_not_found"},
        )

        released = self.coordinator.release_mutation_lease(first["lease_id"])
        duplicate = self.coordinator.release_mutation_lease(first["lease_id"])
        self.assertEqual(released, duplicate)
        self.assertEqual(released["state"], "released")
        self.assertTrue(self.coordinator.mutation_allowed())

    def test_expired_mutation_lease_cannot_be_renewed_or_released(self):
        first = self.coordinator.acquire_mutation_lease("gateway-protocol-operation")
        self.clock[0] += 61

        self.assertEqual(
            self.coordinator.renew_mutation_lease(first["lease_id"]),
            {"ok": False, "error_code": "lease_not_found"},
        )
        self.assertEqual(
            self.coordinator.release_mutation_lease(first["lease_id"]),
            {"ok": False, "error_code": "lease_not_found"},
        )
        self.assertTrue(self.coordinator.mutation_allowed())
        second = self.coordinator.acquire_mutation_lease("next-protocol-operation")
        self.assertNotEqual(first["lease_id"], second["lease_id"])
        self.assertEqual(
            self.coordinator.release_mutation_lease(first["lease_id"]),
            {"ok": False, "error_code": "lease_not_found"},
        )

    def test_explicit_rollback_restores_previous_and_is_idempotent(self):
        self.stage()

        first = self.coordinator.rollback("op-safe-1", self.executor.restore)
        replay = self.coordinator.rollback("op-safe-1", self.executor.restore)

        self.assertEqual(first["state"], "rolled_back")
        self.assertEqual(replay, first)
        self.assertEqual(self.executor.calls.count(("restore", "old-main")), 1)

    def test_persistence_is_atomic_and_excludes_node_configuration(self):
        self.stage()

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        serialized = json.dumps(stored, sort_keys=True)
        self.assertNotIn("config_text", serialized)
        self.assertNotIn("must-not-be-persisted", serialized)
        self.assertEqual(stored["active"]["previous"]["candidate_id"], "old-main")
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())


class ManagerMainAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_coordinator = manager.main_assignment_coordinator
        self.original_active_id = manager.active_openvpn_node_id
        self.original_active_process = manager.active_openvpn_process
        self.original_is_connecting = manager.is_connecting
        manager.main_assignment_coordinator = MainAssignmentCoordinator(
            Path(self.temp.name) / "assignment.json",
            operation_id_factory=lambda: "manager-op-1",
        )
        manager.active_openvpn_node_id = "old-main"
        self.nodes = [
            {
                "id": "old-main",
                "country_short": "US",
                "country": "United States",
                "ip_type": "hosting",
                "probe_status": "available",
                "config_text": "client\nremote old-main.example 1194\n",
            },
            {
                "id": "new-main",
                "country_short": "JP",
                "country": "Japan",
                "ip_type": "hosting",
                "probe_status": "available",
                "config_text": "client\nremote new-main.example 1194\n",
            },
            {
                "id": "standby",
                "country_short": "KR",
                "country": "Korea",
                "ip_type": "hosting",
                "probe_status": "available",
                "config_text": "client\nremote standby.example 1194\n",
            },
        ]

    def tearDown(self):
        manager.main_assignment_coordinator = self.original_coordinator
        manager.active_openvpn_node_id = self.original_active_id
        manager.active_openvpn_process = self.original_active_process
        manager.is_connecting = self.original_is_connecting
        self.temp.cleanup()

    def _start_repair_required(self):
        return manager.main_assignment_coordinator.stage(
            candidate_id="new-main",
            country="JP",
            proxy_type="datacenter",
            expected_current_candidate_id="old-main",
            idempotency_key="gateway-repair-required",
            current=current_snapshot(),
            slot_candidate_ids=set(),
            stage_candidate=lambda _candidate: {
                "dns_verified": False,
                "exit_verified": False,
                "available": False,
            },
            restore_previous=lambda _previous: {
                "dns_verified": False,
                "exit_verified": False,
                "available": False,
            },
        )

    def test_manager_stages_commits_and_exposes_safe_main_identity(self):
        settings = {
            "connection_enabled": True,
            "routing_mode": "fixed_ip",
            "routing_ip_type": "all",
            "fixed_node_id": "old-main",
        }
        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "load_ui_config", return_value=settings),
            mock.patch.object(manager, "current_slot_node_ids", return_value={"slot-one"}),
            mock.patch.object(manager, "connect_node") as connect,
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(
                manager,
                "check_proxy_health",
                return_value={"ok": True, "ip": "198.51.100.90", "latency_ms": 25},
            ),
        ):
            staged = manager.stage_main_assignment(
                "new-main", "JP", "datacenter", "old-main", "gateway-op"
            )
            committed = manager.commit_main_assignment("manager-op-1")

        self.assertEqual(staged["state"], "pending_commit")
        self.assertEqual(committed["state"], "committed")
        connect.assert_called_once_with("new-main")

        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(
                manager,
                "get_state",
                return_value={"proxy_ok": True, "proxy_ip": "198.51.100.90", "proxy_port": 7928},
            ),
        ):
            manager.active_openvpn_node_id = "new-main"
            status = manager.safe_main_status()
        self.assertEqual(status["candidate_id"], "new-main")
        self.assertNotIn("config_text", status)

    def test_safe_main_status_normalizes_real_node_ip_type(self):
        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(
                manager,
                "get_state",
                return_value={"proxy_ok": True, "proxy_ip": "198.51.100.90", "proxy_port": 7928},
            ),
        ):
            status = manager.safe_main_status()

        self.assertEqual(status["proxy_type"], "datacenter")

    def test_manager_rollback_reconnects_previous_main_and_restores_settings(self):
        settings = {
            "connection_enabled": True,
            "routing_mode": "fixed_ip",
            "routing_ip_type": "all",
            "fixed_node_id": "old-main",
        }
        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "load_ui_config", return_value=settings),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "connect_node"),
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "check_proxy_health", return_value={"ok": True, "ip": "198.51.100.90"}),
        ):
            manager.stage_main_assignment(
                "new-main", "JP", "datacenter", "old-main", "gateway-op"
            )

        restored_settings = {}
        with (
            mock.patch.object(manager, "load_ui_config", return_value={}),
            mock.patch.object(
                manager,
                "_write_ui_config_atomic",
                side_effect=lambda value: restored_settings.update(value),
            ),
            mock.patch.object(manager, "connect_node") as connect,
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "check_proxy_health", return_value={"ok": True, "ip": "198.51.100.80"}),
        ):
            result = manager.rollback_main_assignment("manager-op-1")

        self.assertEqual(result["state"], "rolled_back")
        connect.assert_called_once_with("old-main")
        self.assertEqual(restored_settings["fixed_node_id"], "old-main")

    def test_manager_restore_clears_startup_connecting_placeholder(self):
        previous = {
            "candidate_id": "old-main",
            "connection_enabled": True,
            "routing_mode": "auto",
            "routing_ip_type": "all",
            "fixed_node_id": "",
        }
        original_connecting = manager.is_connecting
        manager.is_connecting = True
        try:
            with (
                mock.patch.object(manager, "load_ui_config", return_value={}),
                mock.patch.object(manager, "_write_ui_config_atomic"),
                mock.patch.object(
                    manager,
                    "connect_node",
                    side_effect=lambda _candidate: self.assertFalse(manager.is_connecting),
                ) as connect,
                mock.patch.object(manager, "active_openvpn_running", return_value=True),
                mock.patch.object(
                    manager,
                    "check_proxy_health",
                    return_value={"ok": True, "ip": "198.51.100.80"},
                ),
            ):
                result = manager._restore_main_candidate(previous)
        finally:
            manager.is_connecting = original_connecting

        self.assertTrue(result["available"])
        connect.assert_called_once_with("old-main")

    def test_manager_repair_commit_clears_startup_placeholder_and_stages_new_main(self):
        self.assertEqual(
            manager.main_assignment_coordinator.stage(
                candidate_id="new-main",
                country="JP",
                proxy_type="datacenter",
                expected_current_candidate_id="old-main",
                idempotency_key="gateway-op",
                current={**current_snapshot(), "restorable": True},
                slot_candidate_ids=set(),
                stage_candidate=lambda _candidate: {
                    "dns_verified": False,
                    "exit_verified": False,
                    "available": False,
                },
                restore_previous=lambda _previous: {
                    "dns_verified": False,
                    "exit_verified": False,
                    "available": False,
                },
            )["state"],
            "repair_required",
        )
        original_connecting = manager.is_connecting
        manager.is_connecting = True
        try:
            with (
                mock.patch.object(
                    manager,
                    "connect_node",
                    side_effect=lambda _candidate: self.assertFalse(manager.is_connecting),
                ) as connect,
                mock.patch.object(manager, "active_openvpn_running", return_value=True),
                mock.patch.object(
                    manager,
                    "check_proxy_health",
                    return_value={"ok": True, "ip": "198.51.100.90"},
                ),
            ):
                result = manager.repair_commit_main_assignment("manager-op-1")
        finally:
            manager.is_connecting = original_connecting

        self.assertEqual(result["state"], "pending_gateway_validation")
        connect.assert_called_once_with("new-main")

    def test_transaction_reserved_candidates_survive_unavailable_pool_sort(self):
        self.assertEqual(
            manager.main_assignment_coordinator.stage(
                candidate_id="new-main",
                country="JP",
                proxy_type="datacenter",
                expected_current_candidate_id="old-main",
                idempotency_key="gateway-op",
                current={**current_snapshot(), "restorable": True},
                slot_candidate_ids=set(),
                stage_candidate=lambda _candidate: {
                    "dns_verified": False,
                    "exit_verified": False,
                    "available": False,
                },
                restore_previous=lambda _previous: {
                    "dns_verified": False,
                    "exit_verified": False,
                    "available": False,
                },
            )["state"],
            "repair_required",
        )
        unavailable = [
            {**item, "probe_status": "unavailable"}
            for item in self.nodes
            if item["id"] in {"old-main", "new-main"}
        ]
        manager.active_openvpn_node_id = ""

        retained = manager.sort_all_nodes(unavailable)

        self.assertEqual({item["id"] for item in retained}, {"old-main", "new-main"})

    def test_manager_repair_replace_uses_an_available_unassigned_candidate(self):
        self.assertEqual(
            manager.main_assignment_coordinator.stage(
                candidate_id="new-main",
                country="JP",
                proxy_type="datacenter",
                expected_current_candidate_id="old-main",
                idempotency_key="gateway-op",
                current={**current_snapshot(), "restorable": True},
                slot_candidate_ids=set(),
                stage_candidate=lambda _candidate: {
                    "dns_verified": False,
                    "exit_verified": False,
                    "available": False,
                },
                restore_previous=lambda _previous: {
                    "dns_verified": False,
                    "exit_verified": False,
                    "available": False,
                },
            )["state"],
            "repair_required",
        )
        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "current_slot_node_ids", return_value={"old-main"}),
            mock.patch.object(manager, "connect_node") as connect,
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(
                manager,
                "check_proxy_health",
                return_value={"ok": True, "ip": "198.51.100.90"},
            ),
        ):
            result = manager.repair_replace_main_assignment(
                "manager-op-1", "standby", "KR", "datacenter"
            )

        self.assertEqual(result["state"], "pending_gateway_validation")
        self.assertEqual(result["resolution"], "repair_replace")
        connect.assert_called_once_with("standby")

    def test_repair_entrypoints_share_maintenance_and_slot_lock_boundary(self):
        for method in ("repair_commit", "repair_replace"):
            for busy_lock in (manager.maintenance_lock, manager.exit_slots_supervise_lock):
                with self.subTest(method=method, lock=busy_lock):
                    manager.is_connecting = True
                    busy_lock.acquire()
                    try:
                        if method == "repair_commit":
                            result = manager.repair_commit_main_assignment("manager-op-1")
                        else:
                            with mock.patch.object(manager, "read_nodes", return_value=self.nodes), mock.patch.object(
                                manager, "current_slot_node_ids", return_value=set()
                            ):
                                result = manager.repair_replace_main_assignment(
                                    "manager-op-1", "standby", "KR", "datacenter"
                                )
                    finally:
                        busy_lock.release()
                    self.assertEqual(
                        {"ok": False, "error_code": "operation_busy"}, result
                    )
                    self.assertTrue(manager.is_connecting)

    def test_invalid_or_completed_repair_does_not_clear_connecting_flag(self):
        manager.is_connecting = True
        missing = manager.repair_commit_main_assignment("missing-operation")
        self.assertEqual("operation_not_found", missing["error_code"])
        self.assertTrue(manager.is_connecting)

        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "load_ui_config", return_value={}),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "connect_node"),
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "check_proxy_health", return_value={"ok": True, "ip": "198.51.100.90"}),
        ):
            manager.stage_main_assignment(
                "new-main", "JP", "datacenter", "old-main", "gateway-op"
            )
            manager.commit_main_assignment("manager-op-1")
        manager.is_connecting = True

        completed = manager.repair_commit_main_assignment("manager-op-1")

        self.assertEqual("operation_state_conflict", completed["error_code"])
        self.assertTrue(manager.is_connecting)

    def test_expired_repair_does_not_clear_connecting_flag(self):
        clock = [1_700_000_000.0]
        manager.main_assignment_coordinator = MainAssignmentCoordinator(
            Path(self.temp.name) / "expired-assignment.json",
            now=lambda: clock[0],
            operation_id_factory=lambda: "manager-op-1",
        )
        manager.main_assignment_coordinator.stage(
            candidate_id="new-main",
            country="JP",
            proxy_type="datacenter",
            expected_current_candidate_id="old-main",
            idempotency_key="gateway-op",
            current={**current_snapshot(), "restorable": True},
            slot_candidate_ids=set(),
            stage_candidate=lambda _candidate: {},
            restore_previous=lambda _previous: {},
        )
        manager.main_assignment_coordinator.repair_commit(
            "manager-op-1",
            lambda _candidate: {
                "dns_verified": True,
                "exit_verified": True,
                "available": True,
            },
        )
        clock[0] += 181
        manager.is_connecting = True

        result = manager.repair_commit_main_assignment("manager-op-1")

        self.assertEqual("operation_expired", result["error_code"])
        self.assertTrue(manager.is_connecting)

    def test_concurrent_repair_commit_only_activates_once(self):
        self.assertEqual(
            "repair_required",
            manager.main_assignment_coordinator.stage(
                candidate_id="new-main",
                country="JP",
                proxy_type="datacenter",
                expected_current_candidate_id="old-main",
                idempotency_key="gateway-op",
                current={**current_snapshot(), "restorable": True},
                slot_candidate_ids=set(),
                stage_candidate=lambda _candidate: {},
                restore_previous=lambda _previous: {},
            )["state"],
        )
        entered = threading.Event()
        release = threading.Event()
        results = []

        def connect(_candidate):
            entered.set()
            self.assertTrue(release.wait(2))

        def run_repair():
            results.append(manager.repair_commit_main_assignment("manager-op-1"))

        with (
            mock.patch.object(manager, "connect_node", side_effect=connect) as connect_mock,
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "check_proxy_health", return_value={"ok": True, "ip": "198.51.100.90"}),
        ):
            first = threading.Thread(target=run_repair)
            first.start()
            self.assertTrue(entered.wait(2))
            second = threading.Thread(target=run_repair)
            second.start()
            second.join(2)
            release.set()
            first.join(2)

        self.assertEqual(1, connect_mock.call_count)
        self.assertEqual(
            ["operation_busy", "pending_gateway_validation"],
            sorted(result.get("error_code") or result.get("state") for result in results),
        )

    def test_candidate_snapshot_excludes_main_pending_and_slot_candidates(self):
        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "load_ui_config", return_value={}),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "connect_node"),
            mock.patch.object(manager, "active_openvpn_running", return_value=True),
            mock.patch.object(manager, "check_proxy_health", return_value={"ok": True, "ip": "198.51.100.90"}),
        ):
            manager.stage_main_assignment(
                "new-main", "JP", "datacenter", "old-main", "gateway-op"
            )
        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "current_slot_node_ids", return_value={"standby"}),
        ):
            candidates = manager.safe_candidate_snapshot()

        self.assertEqual(candidates, [])

    def test_persisted_slot_candidates_are_reserved_before_runtime_rebuild(self):
        slots_path = Path(self.temp.name) / "slots.json"
        slots_path.write_text(
            json.dumps(
                {
                    "slots": [
                        {"slot": 0, "node_id": "persisted-slot-zero"},
                        {"slot": 1, "node_id": "persisted-slot-one"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch.object(manager, "SLOTS_FILE", slots_path),
            mock.patch.object(manager, "get_active_slots", return_value=[0, 1]),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "get_slot_pin_map", return_value={}),
        ):
            self.assertEqual(
                manager.reserved_slot_candidate_ids(),
                {"persisted-slot-zero", "persisted-slot-one"},
            )
            self.assertEqual(
                manager.reserved_slot_candidate_ids(exclude_slot=0),
                {"persisted-slot-one"},
            )

    def test_slot_rebuild_prefers_its_own_persisted_healthy_candidate(self):
        slots_path = Path(self.temp.name) / "slots.json"
        slots_path.write_text(
            json.dumps({"slots": [{"slot": 0, "node_id": "persisted-slot-zero"}]}),
            encoding="utf-8",
        )
        candidates = [
            {
                "id": "lower-latency-new",
                "probe_status": "available",
                "country_short": "US",
                "ip_type": "residential",
                "latency_ms": 1,
            },
            {
                "id": "persisted-slot-zero",
                "probe_status": "available",
                "country_short": "US",
                "ip_type": "residential",
                "latency_ms": 50,
            },
        ]
        with (
            mock.patch.object(manager, "SLOTS_FILE", slots_path),
            mock.patch.object(manager, "get_active_slots", return_value=[0]),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "get_slot_pin_map", return_value={}),
            mock.patch.object(manager, "read_nodes", return_value=candidates),
            mock.patch.object(manager, "main_reserved_candidate_ids", return_value=set()),
        ):
            selected = manager.pick_slot_node(0, set())

        self.assertEqual(selected["id"], "persisted-slot-zero")

    def test_mutating_facades_refuse_work_during_pending_main_assignment(self):
        with mock.patch.object(
            manager.main_assignment_coordinator,
            "mutation_allowed",
            return_value=False,
        ):
            self.assertEqual(manager.create_managed_slot("JP", "datacenter"), {"ok": False, "error_code": "operation_busy"})
            self.assertEqual(manager.assign_managed_slot(0, "node", "JP", "datacenter"), {"ok": False, "error_code": "operation_busy"})
            self.assertEqual(manager.rotate_managed_slot(0), {"ok": False, "error_code": "operation_busy"})
            self.assertEqual(manager.delete_managed_slot(0), {"ok": False, "error_code": "operation_busy"})
            self.assertEqual(manager.start_country_refresh("JP")["errorCode"], "operation_busy")

    def test_external_mutation_lease_blocks_all_background_and_ui_mutations(self):
        lease = manager.acquire_mutation_lease("gateway-protocol-operation")
        self.assertTrue(lease["ok"])
        try:
            with (
                mock.patch.object(manager, "_write_ui_config_atomic") as write_ui,
                mock.patch.object(manager, "connect_node") as connect,
                mock.patch.object(manager, "ensure_dirs") as ensure_dirs,
            ):
                self.assertEqual(
                    manager.disconnect_main_connection(),
                    {"ok": False, "error_code": "operation_busy"},
                )
                self.assertIsNone(manager.auto_switch_node())
                self.assertEqual(manager.maintain_valid_nodes(), "operation_busy")
                self.assertEqual(
                    manager.add_one_slot(),
                    {"ok": False, "error_code": "operation_busy"},
                )
                self.assertEqual(
                    manager.start_country_refresh("JP")["errorCode"],
                    "operation_busy",
                )
            write_ui.assert_not_called()
            connect.assert_not_called()
            ensure_dirs.assert_not_called()
        finally:
            manager.release_mutation_lease(lease["lease_id"])

    def test_country_refresh_reserves_mutations_before_worker_handoff(self):
        captured = {}
        original_state = manager.country_refresh_snapshot()

        class DeferredThread:
            def __init__(self, *, target, args, **_kwargs):
                captured["target"] = target
                captured["args"] = args

            def start(self):
                return None

        manager._set_country_refresh(state="idle", country="", phase="")
        try:
            with mock.patch.object(manager.threading, "Thread", DeferredThread):
                accepted = manager.start_country_refresh("JP")

            self.assertEqual(accepted["state"], "running")
            self.assertEqual(
                manager.add_one_slot(),
                {"ok": False, "error_code": "operation_busy"},
            )
            self.assertIn("target", captured)
        finally:
            manager._set_country_refresh(**original_state)
            if manager.maintenance_lock.locked():
                manager.maintenance_lock.release()

    def test_external_mutation_lease_blocks_manual_probe_persistence(self):
        probe_node = {
            **self.nodes[1],
            "remote_host": "new-main.example",
            "remote_port": 1194,
            "ping": 10,
        }
        lease = manager.acquire_mutation_lease("gateway-probe-operation")
        self.assertTrue(lease["ok"])
        try:
            with tempfile.TemporaryDirectory() as probe_temp:
                with (
                    mock.patch.object(manager, "CONFIG_DIR", Path(probe_temp)),
                    mock.patch.object(
                        manager,
                        "test_config_path",
                        return_value=Path(probe_temp) / "probe.ovpn",
                    ),
                    mock.patch.object(manager, "read_nodes", return_value=[probe_node]) as read_nodes,
                    mock.patch.object(manager, "probe_nodes", return_value=[]) as probe_nodes,
                    mock.patch.object(
                        manager,
                        "run_openvpn_until_ready",
                        return_value=(True, "ok", None),
                    ) as run_openvpn,
                    mock.patch.object(manager, "write_json") as write_json,
                    mock.patch.object(manager, "mark_blacklisted") as mark_blacklisted,
                    mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=10),
                    mock.patch.object(manager.vpn_utils, "enrich_ip_info"),
                ):
                    for action in (
                        lambda: manager.test_node_by_id("new-main"),
                        lambda: manager.test_multiple_nodes(["new-main"]),
                    ):
                        with self.subTest(action=action):
                            with self.assertRaisesRegex(RuntimeError, "mutation lease"):
                                action()

            read_nodes.assert_not_called()
            probe_nodes.assert_not_called()
            run_openvpn.assert_not_called()
            write_json.assert_not_called()
            mark_blacklisted.assert_not_called()
        finally:
            manager.release_mutation_lease(lease["lease_id"])

    def test_manual_probe_behavior_is_preserved_without_a_mutation_lease(self):
        probe_node = {
            **self.nodes[1],
            "remote_host": "new-main.example",
            "remote_port": 1194,
            "ping": 10,
        }
        probe_result = {
            **probe_node,
            "probe_status": "available",
            "probe_message": "ok",
            "latency_ms": 10,
        }
        with tempfile.TemporaryDirectory() as probe_temp:
            with (
                mock.patch.object(manager, "CONFIG_DIR", Path(probe_temp)),
                mock.patch.object(
                    manager,
                    "test_config_path",
                    return_value=Path(probe_temp) / "probe.ovpn",
                ),
                mock.patch.object(manager, "read_nodes", return_value=[dict(probe_node)]),
                mock.patch.object(
                    manager,
                    "probe_nodes",
                    return_value=[dict(probe_result)],
                ) as probe_nodes,
                mock.patch.object(
                    manager,
                    "run_openvpn_until_ready",
                    return_value=(True, "ok", None),
                ) as run_openvpn,
                mock.patch.object(manager, "write_json") as write_json,
                mock.patch.object(manager, "mark_blacklisted") as mark_blacklisted,
                mock.patch.object(manager.vpn_utils, "ping_latency_ms", return_value=10),
                mock.patch.object(manager.vpn_utils, "enrich_ip_info"),
            ):
                single = manager.test_node_by_id("new-main")
                batch = manager.test_multiple_nodes(["new-main"])

        self.assertEqual(single["probe_status"], "available")
        self.assertEqual(batch[0]["probe_status"], "available")
        run_openvpn.assert_called_once()
        probe_nodes.assert_called_once()
        self.assertEqual(write_json.call_count, 2)
        mark_blacklisted.assert_not_called()

    def test_stage_refuses_existing_maintenance_or_slot_mutation(self):
        for busy_lock in (manager.maintenance_lock, manager.exit_slots_supervise_lock):
            with self.subTest(lock=busy_lock):
                busy_lock.acquire()
                try:
                    result = manager.stage_main_assignment(
                        "new-main", "JP", "datacenter", "old-main", "gateway-op"
                    )
                finally:
                    busy_lock.release()
                self.assertEqual(result, {"ok": False, "error_code": "operation_busy"})

    def test_recovery_loop_checks_persisted_transaction_before_sleeping(self):
        class StopLoop(Exception):
            pass

        with (
            mock.patch.object(manager, "recover_main_assignment", return_value={"ok": True}) as recover,
            mock.patch.object(manager.time, "sleep", side_effect=StopLoop),
        ):
            with self.assertRaises(StopLoop):
                manager.main_assignment_recovery_loop()

        recover.assert_called_once_with()

    def test_active_main_stage_lease_blocks_low_level_disconnect(self):
        entered = threading.Event()
        release = threading.Event()
        result = []

        def blocking_stage(_candidate):
            entered.set()
            release.wait(1)
            return {"dns_verified": True, "exit_verified": True, "available": True}

        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "_main_connection_snapshot", return_value=current_snapshot()),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "_stage_main_candidate", side_effect=blocking_stage),
            mock.patch.object(manager, "cleanup_policy_routing"),
            mock.patch.object(manager, "stop_process") as stop,
            mock.patch.object(manager, "kill_existing_openvpn_processes"),
        ):
            worker = threading.Thread(
                target=lambda: result.append(
                    manager.stage_main_assignment(
                        "new-main", "JP", "datacenter", "old-main", "gateway-op"
                    )
                )
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(RuntimeError):
                manager.stop_active_openvpn()
            release.set()
            worker.join(1)

        self.assertEqual(result[0]["state"], "pending_commit")
        stop.assert_not_called()

    def test_slot_facades_reject_the_current_main_candidate(self):
        manager.active_openvpn_node_id = "new-main"
        with (
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "get_active_slots", return_value=[0]),
            mock.patch.object(manager, "add_slot_with_node", return_value={"ok": True, "slot": 0}) as add,
            mock.patch.object(manager, "assign_node_to_slot", return_value={"ok": True, "slot": 0}) as assign,
            mock.patch.object(manager, "managed_slot_snapshot", return_value={"ok": True, "slot": 0, "node_id": "new-main"}),
            mock.patch.object(manager, "exit_slots", {0: {"node_id": "old-slot"}}),
        ):
            created = manager.create_managed_slot("JP", "datacenter", "new-main")
            assigned = manager.assign_managed_slot(0, "new-main", "JP", "datacenter")

        self.assertEqual(created, {"ok": False, "error_code": "candidate_in_use"})
        self.assertEqual(assigned, {"ok": False, "error_code": "candidate_in_use"})
        add.assert_not_called()
        assign.assert_not_called()

    def test_automatic_slot_selection_excludes_main_and_transaction_candidates(self):
        manager.active_openvpn_node_id = "old-main"
        selected = []
        candidates = [
            {**self.nodes[0], "country_short": "JP", "latency_ms": 1},
            {**self.nodes[1], "latency_ms": 2},
            {**self.nodes[2], "country_short": "JP", "latency_ms": 3},
        ]
        with (
            mock.patch.object(manager, "read_nodes", return_value=candidates),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(
                manager.main_assignment_coordinator,
                "reserved_candidate_ids",
                return_value={"old-main", "new-main"},
            ),
            mock.patch.object(
                manager,
                "add_slot_with_node",
                side_effect=lambda node_id: selected.append(node_id) or {"ok": True, "slot": 0},
            ),
            mock.patch.object(manager, "set_slot_country"),
            mock.patch.object(manager, "set_slot_type"),
            mock.patch.object(
                manager,
                "managed_slot_snapshot",
                return_value={"ok": True, "slot": 0, "node_id": "standby"},
            ),
        ):
            result = manager.create_managed_slot("JP", "datacenter")

        self.assertTrue(result["ok"])
        self.assertEqual(selected, ["standby"])

    def test_main_explicit_and_automatic_selection_exclude_slot_candidates(self):
        manager.active_openvpn_node_id = "old-main"
        manager.is_connecting = False
        nodes = [
            {**self.nodes[1], "latency_ms": 1, "active": False, "config_file": str(Path(self.temp.name) / "new.ovpn")},
            {**self.nodes[2], "latency_ms": 2, "active": False},
        ]
        with (
            mock.patch.object(manager, "current_slot_node_ids", return_value={"new-main"}),
            mock.patch.object(manager, "read_nodes", return_value=nodes),
            mock.patch.object(manager, "load_ui_config", return_value={"connection_enabled": True, "routing_mode": "auto", "routing_ip_type": "all"}),
            mock.patch.object(manager, "set_state"),
            mock.patch.object(manager, "stop_active_openvpn") as stop,
            mock.patch.object(manager, "run_openvpn_until_ready", return_value=(False, "dial failed", None)),
        ):
            with self.assertRaises(RuntimeError):
                manager.connect_node("new-main")
            with mock.patch.object(manager, "connect_node") as connect:
                manager.auto_switch_node()

        stop.assert_not_called()
        connect.assert_called_once_with("standby")

    def test_pinned_slot_candidates_are_reserved_before_runtime_exists(self):
        manager.active_openvpn_node_id = "old-main"
        with (
            mock.patch.object(manager, "get_slot_pin_map", return_value={"7": "new-main"}),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "_main_connection_snapshot", return_value=current_snapshot()),
            mock.patch.object(manager, "_stage_main_candidate") as stage_candidate,
        ):
            staged = manager.stage_main_assignment(
                "new-main", "JP", "datacenter", "old-main", "gateway-pin-stage"
            )
            candidates = manager.safe_candidate_snapshot()

        self.assertEqual(staged, {"ok": False, "error_code": "candidate_in_use"})
        self.assertNotIn("new-main", {item["id"] for item in candidates})
        stage_candidate.assert_not_called()

    def test_slot_assignment_rejects_a_candidate_pinned_to_another_slot(self):
        with (
            mock.patch.object(manager, "get_slot_pin_map", return_value={"1": "standby"}),
            mock.patch.object(manager, "current_slot_node_ids", return_value=set()),
            mock.patch.object(manager, "get_active_slots", return_value=[0, 1]),
            mock.patch.object(manager, "read_nodes", return_value=self.nodes),
            mock.patch.object(manager, "set_slot_pin") as set_pin,
            mock.patch.object(manager, "tear_down_slot") as tear_down,
            mock.patch.object(manager, "bring_up_slot") as bring_up,
        ):
            result = manager.assign_node_to_slot(0, "standby")

        self.assertEqual(result, {"ok": False, "error_code": "candidate_in_use"})
        set_pin.assert_not_called()
        tear_down.assert_not_called()
        bring_up.assert_not_called()


if __name__ == "__main__":
    unittest.main()
