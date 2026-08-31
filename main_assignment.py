"""主连接两阶段切换的安全状态机。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable


Validation = dict[str, Any]
StageCandidate = Callable[[str], Validation]
RestorePrevious = Callable[[dict[str, Any]], Validation]

_BLOCKING_STATES = {
    "switching",
    "pending_commit",
    "pending_gateway_validation",
    "rolling_back",
    "repairing",
    "repair_required",
}
_OPERATION_STATES = _BLOCKING_STATES | {"committed", "rolled_back"}
_ACTIVE_OPERATION_STATES = set(_BLOCKING_STATES)
_LEASE_STATES = {"active", "released", "expired"}
_LEGACY_TERMINAL_RESOLUTIONS = {
    "committed_new_after_repair",
    "replaced_after_repair",
}
_OPERATION_REQUIRED_FIELDS = {
    "ok",
    "operation_id",
    "state",
    "old_candidate_id",
    "new_candidate_id",
    "country",
    "proxy_type",
    "port",
    "dns_verified",
    "exit_verified",
    "available",
    "error_code",
    "created_at",
    "expires_at",
    "idempotency_hash",
    "request_hash",
    "previous",
}
_OPERATION_OPTIONAL_FIELDS = {"repair_request_hash", "resolution"}
_PREVIOUS_FIELDS = {
    "candidate_id",
    "country",
    "proxy_type",
    "connection_enabled",
    "routing_mode",
    "routing_ip_type",
    "fixed_node_id",
}


class MainAssignmentCoordinator:
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        operation_id_factory: Callable[[], str] | None = None,
        ttl_seconds: int = 180,
        mutation_lease_ttl_seconds: int = 60,
    ):
        self.path = Path(path)
        self.now = now
        self.operation_id_factory = operation_id_factory or (lambda: secrets.token_hex(16))
        self.ttl_seconds = ttl_seconds
        self.mutation_lease_ttl_seconds = mutation_lease_ttl_seconds
        self._lock = threading.RLock()
        self._live_operation_ids: set[str] = set()
        self._state_migration_required = False
        self._state = self._read()
        if self._state_migration_required:
            self._write()

    def mutation_allowed(self) -> bool:
        with self._lock:
            self._expire_mutation_lease_locked()
            active = self._state.get("active")
            lease = self._state.get("mutation_lease")
            return (
                (not isinstance(active, dict) or active.get("state") not in _BLOCKING_STATES)
                and (not isinstance(lease, dict) or lease.get("state") != "active")
            )

    def assignment_action_allowed(self) -> bool:
        with self._lock:
            self._expire_mutation_lease_locked()
            lease = self._state.get("mutation_lease")
            return not isinstance(lease, dict) or lease.get("state") != "active"

    def acquire_mutation_lease(self, idempotency_key: str) -> dict[str, Any]:
        key = str(idempotency_key or "")
        if (
            not 8 <= len(key) <= 256
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in key)
        ):
            return {"ok": False, "error_code": "invalid_request"}
        key_hash = self._digest(key)
        with self._lock:
            self._expire_mutation_lease_locked()
            lease = self._state.get("mutation_lease")
            if isinstance(lease, dict) and lease.get("state") == "active":
                if lease.get("idempotency_hash") == key_hash:
                    return self._public_mutation_lease(lease)
                return {"ok": False, "error_code": "lease_busy"}
            active = self._state.get("active")
            if isinstance(active, dict) and active.get("state") in _BLOCKING_STATES:
                return {"ok": False, "error_code": "operation_busy"}
            lease = {
                "ok": True,
                "state": "active",
                "lease_id": secrets.token_urlsafe(32),
                "idempotency_hash": key_hash,
                "expires_at": self.now() + self.mutation_lease_ttl_seconds,
            }
            self._state["mutation_lease"] = lease
            self._write()
            return self._public_mutation_lease(lease)

    def renew_mutation_lease(self, lease_id: str) -> dict[str, Any]:
        wanted = str(lease_id or "")
        with self._lock:
            self._expire_mutation_lease_locked()
            lease = self._state.get("mutation_lease")
            if (
                not isinstance(lease, dict)
                or lease.get("state") != "active"
                or not secrets.compare_digest(str(lease.get("lease_id") or ""), wanted)
            ):
                return {"ok": False, "error_code": "lease_not_found"}
            lease["expires_at"] = self.now() + self.mutation_lease_ttl_seconds
            self._state["mutation_lease"] = lease
            self._write()
            return self._public_mutation_lease(lease)

    def release_mutation_lease(self, lease_id: str) -> dict[str, Any]:
        wanted = str(lease_id or "")
        with self._lock:
            lease = self._state.get("mutation_lease")
            if (
                not isinstance(lease, dict)
                or not secrets.compare_digest(str(lease.get("lease_id") or ""), wanted)
            ):
                return {"ok": False, "error_code": "lease_not_found"}
            if lease.get("state") == "released":
                return {"ok": True, "state": "released"}
            if lease.get("state") != "active" or self.now() >= float(lease.get("expires_at") or 0):
                return {"ok": False, "error_code": "lease_not_found"}
            lease["state"] = "released"
            lease.pop("expires_at", None)
            self._state["mutation_lease"] = lease
            self._write()
            return {"ok": True, "state": "released"}

    def reserved_candidate_ids(self) -> set[str]:
        with self._lock:
            active = self._state.get("active")
            if not isinstance(active, dict) or active.get("state") not in _BLOCKING_STATES:
                return set()
            return {
                candidate_id
                for field in ("old_candidate_id", "new_candidate_id")
                if (candidate_id := str(active.get(field) or "").strip())
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = self._state.get("active")
            if isinstance(active, dict):
                return self._public(active)
            return {"ok": True, "state": "idle"}

    def stage(
        self,
        *,
        candidate_id: str,
        country: str,
        proxy_type: str,
        expected_current_candidate_id: str,
        idempotency_key: str,
        current: dict[str, Any],
        slot_candidate_ids: set[str],
        stage_candidate: StageCandidate,
        restore_previous: RestorePrevious,
    ) -> dict[str, Any]:
        request = {
            "candidate_id": str(candidate_id or "").strip(),
            "country": str(country or "").strip().upper(),
            "proxy_type": str(proxy_type or "").strip(),
            "expected_current_candidate_id": str(expected_current_candidate_id or "").strip(),
        }
        idempotency_hash = self._digest(str(idempotency_key or ""))
        request_hash = self._digest(json.dumps(request, sort_keys=True, separators=(",", ":")))
        with self._lock:
            previous_result = self._state["operations"].get(idempotency_hash)
            if isinstance(previous_result, dict):
                if previous_result.get("request_hash") != request_hash:
                    return {"ok": False, "error_code": "idempotency_conflict"}
                return self._public(previous_result)
            current_id = str(current.get("candidate_id") or "").strip()
            if request["expected_current_candidate_id"] != current_id:
                return {"ok": False, "error_code": "current_mismatch"}
            if current.get("restorable") is not True:
                return {"ok": False, "error_code": "current_not_restorable"}
            if request["candidate_id"] in {str(value or "").strip() for value in slot_candidate_ids}:
                return {"ok": False, "error_code": "candidate_in_use"}
            if not self.mutation_allowed():
                return {"ok": False, "error_code": "operation_busy"}
            previous = {
                key: current[key]
                for key in _PREVIOUS_FIELDS
                if key in current
            }
            operation = {
                "ok": True,
                "operation_id": self.operation_id_factory(),
                "state": "switching",
                "old_candidate_id": current_id,
                "new_candidate_id": request["candidate_id"],
                "country": request["country"],
                "proxy_type": request["proxy_type"],
                "port": 7928,
                "dns_verified": False,
                "exit_verified": False,
                "available": False,
                "error_code": "",
                "created_at": self.now(),
                "expires_at": self.now() + self.ttl_seconds,
                "idempotency_hash": idempotency_hash,
                "request_hash": request_hash,
                "previous": previous,
            }
            self._save_operation(operation, active=True)
            self._live_operation_ids.add(operation["operation_id"])

        try:
            validation = stage_candidate(request["candidate_id"])
        except Exception:
            validation = {}
        with self._lock:
            active = self._state.get("active")
            if (
                not isinstance(active, dict)
                or active.get("operation_id") != operation["operation_id"]
                or active.get("state") != "switching"
            ):
                self._live_operation_ids.discard(operation["operation_id"])
                return self._public(active) if isinstance(active, dict) else {"ok": False, "error_code": "operation_state_conflict"}
            operation.update(self._validation(validation))
            if self._verified(operation):
                operation["state"] = "pending_commit"
                self._save_operation(operation, active=True)
                self._live_operation_ids.discard(operation["operation_id"])
                return self._public(operation)
        try:
            return self._restore(
                operation,
                restore_previous,
                success_error="assign_failed_rolled_back",
            )
        finally:
            with self._lock:
                self._live_operation_ids.discard(operation["operation_id"])

    def commit(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            if not self.assignment_action_allowed():
                return {"ok": False, "error_code": "operation_busy"}
            operation = self._find_operation(operation_id)
            if operation is None:
                return {"ok": False, "error_code": "operation_not_found"}
            if operation.get("state") == "committed":
                return self._public(operation)
            if (
                operation.get("state") in {"pending_commit", "pending_gateway_validation"}
                and self.now() >= float(operation.get("expires_at") or 0)
            ):
                return {"ok": False, "error_code": "operation_expired"}
            active = self._state.get("active")
            if (
                operation.get("state") not in {"pending_commit", "pending_gateway_validation"}
                or not isinstance(active, dict)
                or active.get("operation_id") != operation_id
            ):
                return {"ok": False, "error_code": "operation_state_conflict"}
            operation["state"] = "committed"
            operation["error_code"] = ""
            self._save_operation(operation, active=False)
            return self._public(operation)

    def rollback(
        self,
        operation_id: str,
        restore_previous: RestorePrevious,
    ) -> dict[str, Any]:
        with self._lock:
            operation = self._find_operation(operation_id)
            if operation is None:
                return {"ok": False, "error_code": "operation_not_found"}
            if operation.get("state") == "rolled_back":
                return self._public(operation)
            active = self._state.get("active")
            if (
                operation.get("state")
                not in {
                    "switching",
                    "pending_commit",
                    "pending_gateway_validation",
                    "rolling_back",
                    "repair_required",
                }
                or not isinstance(active, dict)
                or active.get("operation_id") != operation_id
            ):
                return {"ok": False, "error_code": "operation_state_conflict"}
        return self._restore(operation, restore_previous, success_error="")

    def repair_commit(
        self,
        operation_id: str,
        activate_candidate: StageCandidate,
    ) -> dict[str, Any]:
        return self._repair_candidate(
            operation_id,
            activate_candidate,
            replacement=None,
            repair_request={"action": "repair_commit"},
            resolution="repair_commit",
            failure_code="repair_commit_failed",
        )

    def repair_replace(
        self,
        operation_id: str,
        candidate_id: str,
        country: str,
        proxy_type: str,
        activate_candidate: StageCandidate,
    ) -> dict[str, Any]:
        replacement = {
            "new_candidate_id": str(candidate_id or "").strip(),
            "country": str(country or "").strip().upper(),
            "proxy_type": str(proxy_type or "").strip(),
        }
        if (
            not replacement["new_candidate_id"]
            or len(replacement["country"]) != 2
            or replacement["proxy_type"] not in {"residential", "datacenter"}
        ):
            return {"ok": False, "error_code": "invalid_request"}
        return self._repair_candidate(
            operation_id,
            activate_candidate,
            replacement=replacement,
            repair_request={"action": "repair_replace", **replacement},
            resolution="repair_replace",
            failure_code="repair_replace_failed",
        )

    def _repair_candidate(
        self,
        operation_id: str,
        activate_candidate: StageCandidate,
        *,
        replacement: dict[str, str] | None,
        repair_request: dict[str, str],
        resolution: str,
        failure_code: str,
    ) -> dict[str, Any]:
        repair_request_hash = self._digest(
            json.dumps(repair_request, sort_keys=True, separators=(",", ":"))
        )
        with self._lock:
            operation = self._find_operation(operation_id)
            if operation is None:
                return {"ok": False, "error_code": "operation_not_found"}
            if operation.get("state") in {"pending_gateway_validation", "committed"}:
                stored_repair_hash = operation.get("repair_request_hash")
                if not stored_repair_hash:
                    return {"ok": False, "error_code": "operation_state_conflict"}
                if stored_repair_hash != repair_request_hash:
                    return {"ok": False, "error_code": "idempotency_conflict"}
                if operation.get("state") == "pending_gateway_validation" and self.now() >= float(operation.get("expires_at") or 0):
                    return {"ok": False, "error_code": "operation_expired"}
                return self._public(operation)
            active = self._state.get("active")
            if (
                operation.get("state") != "repair_required"
                or not isinstance(active, dict)
                or active.get("operation_id") != operation_id
            ):
                return {"ok": False, "error_code": "operation_state_conflict"}
            if replacement is not None:
                operation.update(replacement)
            operation["state"] = "repairing"
            operation["repair_request_hash"] = repair_request_hash
            operation["resolution"] = resolution
            operation["expires_at"] = self.now() + self.ttl_seconds
            self._save_operation(operation, active=True)
            self._live_operation_ids.add(operation_id)
        try:
            validation = activate_candidate(str(operation.get("new_candidate_id") or ""))
        except Exception:
            validation = {}
        with self._lock:
            active = self._state.get("active")
            if (
                not isinstance(active, dict)
                or active.get("operation_id") != operation_id
                or active.get("state") != "repairing"
            ):
                self._live_operation_ids.discard(operation_id)
                return self._public(active) if isinstance(active, dict) else {"ok": False, "error_code": "operation_state_conflict"}
            operation.update(self._validation(validation))
            if self._verified(operation):
                operation["state"] = "pending_gateway_validation"
                operation["error_code"] = ""
                operation["ok"] = True
                self._save_operation(operation, active=True)
            else:
                operation["state"] = "repair_required"
                operation["error_code"] = failure_code
                operation["ok"] = False
                operation.pop("repair_request_hash", None)
                operation.pop("resolution", None)
                self._save_operation(operation, active=True)
            self._live_operation_ids.discard(operation_id)
            return self._public(operation)

    def recover(self, restore_previous: RestorePrevious) -> dict[str, Any]:
        with self._lock:
            active = self._state.get("active")
            if not isinstance(active, dict):
                return {"ok": True, "state": "idle"}
            if active.get("state") == "repair_required":
                return self._public(active)
            if (
                active.get("state") in {"switching", "repairing"}
                and active.get("operation_id") in self._live_operation_ids
                and self.now() < float(active.get("expires_at") or 0)
            ):
                return self._public(active)
            if active.get("state") in {"pending_commit", "pending_gateway_validation"} and self.now() < float(active.get("expires_at") or 0):
                return self._public(active)
            operation = dict(active)
        return self._restore(operation, restore_previous, success_error="assignment_expired")

    def _restore(
        self,
        operation: dict[str, Any],
        restore_previous: RestorePrevious,
        *,
        success_error: str,
    ) -> dict[str, Any]:
        with self._lock:
            active = self._state.get("active")
            if (
                not isinstance(active, dict)
                or active.get("operation_id") != operation.get("operation_id")
            ):
                stored = self._find_operation(str(operation.get("operation_id") or ""))
                return self._public(stored) if isinstance(stored, dict) else {"ok": False, "error_code": "operation_state_conflict"}
            if active.get("state") == "rolling_back":
                return self._public(active)
            operation = dict(active)
            operation["state"] = "rolling_back"
            self._save_operation(operation, active=True)
        try:
            validation = restore_previous(dict(operation.get("previous") or {}))
        except Exception:
            validation = {}
        with self._lock:
            active = self._state.get("active")
            if (
                not isinstance(active, dict)
                or active.get("operation_id") != operation.get("operation_id")
                or active.get("state") != "rolling_back"
            ):
                stored = self._find_operation(str(operation.get("operation_id") or ""))
                return self._public(stored) if isinstance(stored, dict) else {"ok": False, "error_code": "operation_state_conflict"}
            operation.update(self._validation(validation))
            if self._verified(operation):
                operation["state"] = "rolled_back"
                operation["error_code"] = success_error
                operation["ok"] = not bool(success_error)
                self._save_operation(operation, active=False)
            else:
                operation["state"] = "repair_required"
                operation["error_code"] = "rollback_failed"
                operation["ok"] = False
                self._save_operation(operation, active=True)
            return self._public(operation)

    def _read(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "active": None, "operations": {}, "mutation_lease": None}
        except OSError:
            return self._corrupt_state()
        try:
            raw = json.loads(text)
            if self._valid_document(raw):
                return raw
            if (
                isinstance(raw, dict)
                and set(raw) == {"version", "active", "operations"}
            ):
                migrated = dict(raw)
                migrated["mutation_lease"] = None
                if self._valid_document(migrated):
                    self._state_migration_required = True
                    return migrated
                migrated = self._canonicalize_legacy_terminal_history(raw)
                if migrated is not None and self._valid_document(migrated):
                    self._state_migration_required = True
                    return migrated
        except (json.JSONDecodeError, KeyError, OverflowError, TypeError, ValueError):
            pass
        return self._corrupt_state()

    @classmethod
    def _canonicalize_legacy_terminal_history(
        cls,
        raw: dict[str, Any],
    ) -> dict[str, Any] | None:
        if raw.get("active") is not None or not isinstance(raw.get("operations"), dict):
            return None
        operations: dict[str, Any] = {}
        canonicalized = False
        for history_key, stored in raw["operations"].items():
            if not isinstance(stored, dict) or stored.get("state") not in {
                "committed",
                "rolled_back",
            }:
                return None
            operation = dict(stored)
            resolution = operation.get("resolution")
            if resolution in _LEGACY_TERMINAL_RESOLUTIONS:
                if (
                    operation.get("state") != "committed"
                    or "repair_request_hash" in operation
                ):
                    return None
                operation.pop("resolution")
                canonicalized = True
            operations[history_key] = operation
        if not canonicalized:
            return None
        return {
            "version": raw.get("version"),
            "active": None,
            "operations": operations,
            "mutation_lease": None,
        }

    @classmethod
    def _valid_document(cls, raw: Any) -> bool:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"version", "active", "operations", "mutation_lease"}
            or type(raw.get("version")) is not int
            or raw.get("version") != 1
            or not isinstance(raw.get("operations"), dict)
        ):
            return False

        operations = raw["operations"]
        operation_ids: set[str] = set()
        blocking_operations: list[dict[str, Any]] = []
        for history_key, operation in operations.items():
            if (
                not cls._valid_hash(history_key)
                or not cls._valid_operation(operation)
                or operation["idempotency_hash"] != history_key
                or operation["operation_id"] in operation_ids
            ):
                return False
            operation_ids.add(operation["operation_id"])
            if operation["state"] in _ACTIVE_OPERATION_STATES:
                blocking_operations.append(operation)

        active = raw["active"]
        if active is None:
            if blocking_operations:
                return False
        elif (
            not cls._valid_operation(active)
            or active["state"] not in _ACTIVE_OPERATION_STATES
            or operations.get(active["idempotency_hash"]) != active
            or len(blocking_operations) != 1
        ):
            return False

        lease = raw["mutation_lease"]
        return lease is None or cls._valid_lease(lease)

    @classmethod
    def _valid_operation(cls, operation: Any) -> bool:
        if (
            not isinstance(operation, dict)
            or not _OPERATION_REQUIRED_FIELDS.issubset(operation)
            or set(operation) - (_OPERATION_REQUIRED_FIELDS | _OPERATION_OPTIONAL_FIELDS)
        ):
            return False
        if (
            type(operation["ok"]) is not bool
            or not cls._valid_identifier(operation["operation_id"], 128)
            or not isinstance(operation["state"], str)
            or operation["state"] not in _OPERATION_STATES
            or not cls._valid_text(operation["old_candidate_id"], 256)
            or not cls._valid_text(operation["new_candidate_id"], 256)
            or not cls._valid_country(operation["country"])
            or not isinstance(operation["proxy_type"], str)
            or operation["proxy_type"] not in {"residential", "datacenter"}
            or type(operation["port"]) is not int
            or operation["port"] != 7928
            or any(
                type(operation[field]) is not bool
                for field in ("dns_verified", "exit_verified", "available")
            )
            or not isinstance(operation["error_code"], str)
            or len(operation["error_code"]) > 128
            or not cls._valid_number(operation["created_at"])
            or not cls._valid_number(operation["expires_at"])
            or operation["expires_at"] < operation["created_at"]
            or not cls._valid_hash(operation["idempotency_hash"])
            or not cls._valid_hash(operation["request_hash"])
            or not cls._valid_previous(operation["previous"])
        ):
            return False

        repair_hash = operation.get("repair_request_hash")
        resolution = operation.get("resolution")
        if (repair_hash is None) != (resolution is None):
            return False
        if repair_hash is not None and (
            not cls._valid_hash(repair_hash)
            or not isinstance(resolution, str)
            or resolution not in {"repair_commit", "repair_replace"}
        ):
            return False
        if operation["state"] in {"repairing", "pending_gateway_validation"}:
            return repair_hash is not None
        return True

    @classmethod
    def _valid_previous(cls, previous: Any) -> bool:
        if (
            not isinstance(previous, dict)
            or "candidate_id" not in previous
            or set(previous) - _PREVIOUS_FIELDS
            or not cls._valid_text(previous["candidate_id"], 256)
        ):
            return False
        if "country" in previous and not (
            previous["country"] == "" or cls._valid_country(previous["country"])
        ):
            return False
        if "proxy_type" in previous:
            proxy_type = previous["proxy_type"]
            if not isinstance(proxy_type, str) or proxy_type not in {
                "",
                "residential",
                "datacenter",
            }:
                return False
        if "connection_enabled" in previous and type(previous["connection_enabled"]) is not bool:
            return False
        if "routing_mode" in previous:
            routing_mode = previous["routing_mode"]
            if not isinstance(routing_mode, str) or routing_mode not in {
                "auto",
                "fixed_ip",
                "fixed_region",
                "favorites",
            }:
                return False
        if "routing_ip_type" in previous:
            routing_ip_type = previous["routing_ip_type"]
            if not isinstance(routing_ip_type, str) or routing_ip_type not in {
                "all",
                "residential",
                "hosting",
            }:
                return False
        return "fixed_node_id" not in previous or (
            isinstance(previous["fixed_node_id"], str)
            and len(previous["fixed_node_id"]) <= 256
        )

    @classmethod
    def _valid_lease(cls, lease: Any) -> bool:
        if (
            not isinstance(lease, dict)
            or not isinstance(lease.get("state"), str)
            or lease.get("state") not in _LEASE_STATES
        ):
            return False
        required = {"ok", "state", "lease_id", "idempotency_hash"}
        if lease["state"] == "active":
            required.add("expires_at")
        if set(lease) != required:
            return False
        return (
            lease["ok"] is True
            and cls._valid_identifier(lease["lease_id"], 128, minimum=32)
            and cls._valid_hash(lease["idempotency_hash"])
            and (
                lease["state"] != "active"
                or cls._valid_number(lease["expires_at"])
            )
        )

    @staticmethod
    def _valid_number(value: Any) -> bool:
        return (
            type(value) in {int, float}
            and math.isfinite(float(value))
            and value >= 0
        )

    @staticmethod
    def _valid_hash(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _valid_text(value: Any, maximum: int) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= maximum

    @classmethod
    def _valid_identifier(
        cls,
        value: Any,
        maximum: int,
        *,
        minimum: int = 1,
    ) -> bool:
        return (
            isinstance(value, str)
            and minimum <= len(value) <= maximum
            and all(character.isascii() and (character.isalnum() or character in "_-") for character in value)
        )

    @staticmethod
    def _valid_country(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 2
            and value.isascii()
            and value.isalpha()
            and value.isupper()
        )

    @staticmethod
    def _corrupt_state() -> dict[str, Any]:
        return {
            "version": 1,
            "active": {"ok": False, "state": "repair_required", "error_code": "state_corrupt"},
            "operations": {},
            "mutation_lease": None,
        }

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                json.dump(self._state, destination, ensure_ascii=False, sort_keys=True)
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _save_operation(self, operation: dict[str, Any], *, active: bool) -> None:
        stored = dict(operation)
        self._state["operations"][stored["idempotency_hash"]] = stored
        self._state["active"] = stored if active else None
        self._write()

    def _find_operation(self, operation_id: str) -> dict[str, Any] | None:
        wanted = str(operation_id or "")
        for operation in self._state["operations"].values():
            if operation.get("operation_id") == wanted:
                return dict(operation)
        return None

    def _expire_mutation_lease_locked(self) -> None:
        lease = self._state.get("mutation_lease")
        if (
            isinstance(lease, dict)
            and lease.get("state") == "active"
            and self.now() >= float(lease.get("expires_at") or 0)
        ):
            lease["state"] = "expired"
            lease.pop("expires_at", None)
            self._state["mutation_lease"] = lease
            self._write()

    @staticmethod
    def _public_mutation_lease(lease: dict[str, Any]) -> dict[str, Any]:
        return {
            key: lease[key]
            for key in ("ok", "state", "lease_id", "expires_at")
            if key in lease
        }

    @staticmethod
    def _validation(value: Any) -> dict[str, bool]:
        value = value if isinstance(value, dict) else {}
        return {
            "dns_verified": value.get("dns_verified") is True,
            "exit_verified": value.get("exit_verified") is True,
            "available": value.get("available") is True,
        }

    @staticmethod
    def _verified(value: dict[str, Any]) -> bool:
        return all(value.get(field) is True for field in ("dns_verified", "exit_verified", "available"))

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _public(operation: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "ok",
            "operation_id",
            "state",
            "old_candidate_id",
            "new_candidate_id",
            "country",
            "proxy_type",
            "port",
            "dns_verified",
            "exit_verified",
            "available",
            "error_code",
            "resolution",
            "expires_at",
        }
        return {key: operation[key] for key in fields if key in operation}
