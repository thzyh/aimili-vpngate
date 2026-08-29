import json
import tempfile
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

    def test_expired_pending_operation_is_rolled_back_after_restart(self):
        self.stage()
        self.clock[0] += 181
        restarted = MainAssignmentCoordinator(self.path, now=lambda: self.clock[0])

        result = restarted.recover(self.executor.restore)

        self.assertEqual(result["state"], "rolled_back")
        self.assertEqual(result["error_code"], "assignment_expired")
        self.assertEqual(self.executor.calls[-1], ("restore", "old-main"))
        self.assertTrue(restarted.mutation_allowed())

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
            },
            {
                "id": "new-main",
                "country_short": "JP",
                "country": "Japan",
                "ip_type": "hosting",
                "probe_status": "available",
            },
            {
                "id": "standby",
                "country_short": "KR",
                "country": "Korea",
                "ip_type": "hosting",
                "probe_status": "available",
            },
        ]

    def tearDown(self):
        manager.main_assignment_coordinator = self.original_coordinator
        manager.active_openvpn_node_id = self.original_active_id
        self.temp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
