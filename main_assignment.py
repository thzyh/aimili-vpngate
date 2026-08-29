"""主连接两阶段切换的安全状态机。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable


Validation = dict[str, Any]
StageCandidate = Callable[[str], Validation]
RestorePrevious = Callable[[dict[str, Any]], Validation]

_BLOCKING_STATES = {"switching", "pending_commit", "rolling_back", "repair_required"}
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
    ):
        self.path = Path(path)
        self.now = now
        self.operation_id_factory = operation_id_factory or (lambda: secrets.token_hex(16))
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._state = self._read()

    def mutation_allowed(self) -> bool:
        with self._lock:
            active = self._state.get("active")
            return not isinstance(active, dict) or active.get("state") not in _BLOCKING_STATES

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

        try:
            validation = stage_candidate(request["candidate_id"])
        except Exception:
            validation = {}
        with self._lock:
            operation.update(self._validation(validation))
            if self._verified(operation):
                operation["state"] = "pending_commit"
                self._save_operation(operation, active=True)
                return self._public(operation)
        return self._restore(
            operation,
            restore_previous,
            success_error="assign_failed_rolled_back",
        )

    def commit(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            operation = self._find_operation(operation_id)
            if operation is None:
                return {"ok": False, "error_code": "operation_not_found"}
            if operation.get("state") == "committed":
                return self._public(operation)
            active = self._state.get("active")
            if (
                operation.get("state") != "pending_commit"
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
                operation.get("state") not in {"switching", "pending_commit", "rolling_back"}
                or not isinstance(active, dict)
                or active.get("operation_id") != operation_id
            ):
                return {"ok": False, "error_code": "operation_state_conflict"}
        return self._restore(operation, restore_previous, success_error="")

    def recover(self, restore_previous: RestorePrevious) -> dict[str, Any]:
        with self._lock:
            active = self._state.get("active")
            if not isinstance(active, dict):
                return {"ok": True, "state": "idle"}
            if active.get("state") == "repair_required":
                return self._public(active)
            if active.get("state") == "pending_commit" and self.now() < float(active.get("expires_at") or 0):
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
            operation["state"] = "rolling_back"
            self._save_operation(operation, active=True)
        try:
            validation = restore_previous(dict(operation.get("previous") or {}))
        except Exception:
            validation = {}
        with self._lock:
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
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("operations"), dict):
                raw.setdefault("active", None)
                return raw
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": 1, "active": None, "operations": {}}

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
            "expires_at",
        }
        return {key: operation[key] for key in fields if key in operation}
