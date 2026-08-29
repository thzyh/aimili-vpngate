"""AimiliVPN 的仅回环、版本化控制 API。"""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


API_PREFIX = "/control/v1"
MAX_REQUEST_BYTES = 16 * 1024
CAPABILITIES = [
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
    "admin.read",
    "admin.verify",
    "admin.update",
    "admin.sessions.issue",
]


class ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager: Any, token: str):
        self.manager = manager
        self.control_token = token
        super().__init__(address, ControlHandler)


class ControlHandler(BaseHTTPRequestHandler):
    server: ControlHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[ControlAPI] {self.log_date_time_string()} {format % args}", flush=True)

    def _send_json(self, status: HTTPStatus, payload: Any = None) -> None:
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: HTTPStatus, code: str) -> None:
        self._send_json(status, {"error": {"code": code}})

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, separator, value = header.partition(" ")
        return (
            separator == " "
            and scheme == "Bearer"
            and bool(value)
            and hmac.compare_digest(value, self.server.control_token)
        )

    def _read_object(self, allowed_fields: set[str]) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid request") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("invalid request")
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid request") from exc
        if not isinstance(payload, dict) or set(payload) - allowed_fields:
            raise ValueError("invalid request")
        return payload

    @staticmethod
    def _slot_route(path: str) -> tuple[int, str] | None:
        match = re.fullmatch(r"/control/v1/slots/(\d+)(?:/(rotate|check|assign))?", path)
        if not match:
            return None
        return int(match.group(1)), match.group(2) or ""

    @staticmethod
    def _main_assignment_action(path: str) -> tuple[str, str] | None:
        match = re.fullmatch(
            r"/control/v1/main/assign/([A-Za-z0-9_-]{8,128})/(commit|rollback)",
            path,
        )
        if not match:
            return None
        return match.group(1), match.group(2)

    def _manager_result(self, result: Any, success: HTTPStatus = HTTPStatus.OK) -> None:
        if isinstance(result, dict) and result.get("ok") is False:
            code = str(result.get("error_code") or "operation_failed")
            if code in ("slot_not_found", "candidate_not_found", "operation_not_found"):
                status = HTTPStatus.NOT_FOUND
            elif code in ("invalid_request", "candidate_mismatch"):
                status = HTTPStatus.BAD_REQUEST
            elif code == "rollback_failed":
                status = HTTPStatus.SERVICE_UNAVAILABLE
            else:
                status = HTTPStatus.CONFLICT
            self._error(status, code)
            return
        if isinstance(result, dict):
            result = dict(result)
            result.pop("ok", None)
        self._send_json(success, {"data": result})

    def _dispatch(self) -> None:
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return

        path = self.path.split("?", 1)[0]
        if self.command == "GET" and path == f"{API_PREFIX}/capabilities":
            self._send_json(
                HTTPStatus.OK,
                {"data": {"apiVersion": "v1", "serviceVersion": "custom-v1", "capabilities": CAPABILITIES}},
            )
            return
        if self.command == "GET" and path == f"{API_PREFIX}/candidates":
            self._manager_result(self.server.manager.safe_candidate_snapshot())
            return
        if self.command == "GET" and path == f"{API_PREFIX}/main":
            self._manager_result(self.server.manager.safe_main_status())
            return
        if self.command == "GET" and path == f"{API_PREFIX}/main/assignment":
            result = self.server.manager.main_assignment_snapshot()
            if not isinstance(result, dict):
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "invalid_response")
                return
            result = dict(result)
            result.pop("ok", None)
            self._send_json(HTTPStatus.OK, {"data": result})
            return
        if self.command == "POST" and path == f"{API_PREFIX}/main/assign":
            payload = self._read_object(
                {
                    "candidateId",
                    "country",
                    "proxyType",
                    "expectedCurrentCandidateId",
                    "idempotencyKey",
                }
            )
            candidate_id = str(payload.get("candidateId") or "").strip()
            country = str(payload.get("country") or "").strip().upper()
            proxy_type = str(payload.get("proxyType") or "").strip().lower()
            expected = str(payload.get("expectedCurrentCandidateId") or "").strip()
            idempotency_key = str(payload.get("idempotencyKey") or "").strip()
            if (
                not candidate_id
                or len(candidate_id) > 256
                or not re.fullmatch(r"[A-Z]{2}", country)
                or proxy_type not in ("residential", "datacenter")
                or not expected
                or len(expected) > 256
                or not 8 <= len(idempotency_key) <= 256
                or any(ord(character) < 0x21 or ord(character) == 0x7F for character in idempotency_key)
            ):
                raise ValueError("invalid request")
            result = self.server.manager.stage_main_assignment(
                candidate_id,
                country,
                proxy_type,
                expected,
                idempotency_key,
            )
            self._manager_result(result, HTTPStatus.ACCEPTED)
            return
        main_action = self._main_assignment_action(path)
        if self.command == "POST" and main_action:
            self._read_object(set())
            operation_id, action = main_action
            method = (
                self.server.manager.commit_main_assignment
                if action == "commit"
                else self.server.manager.rollback_main_assignment
            )
            self._manager_result(method(operation_id))
            return
        if self.command == "GET" and path == f"{API_PREFIX}/candidates/countries":
            self._manager_result(self.server.manager.country_catalog_snapshot())
            return
        if self.command == "GET" and path == f"{API_PREFIX}/candidates/refresh":
            self._manager_result(self.server.manager.country_refresh_snapshot())
            return
        if self.command == "POST" and path == f"{API_PREFIX}/candidates/refresh":
            payload = self._read_object({"country"})
            country = str(payload.get("country") or "").strip().upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                raise ValueError("invalid request")
            result = self.server.manager.start_country_refresh(country)
            if isinstance(result, dict) and result.get("state") == "failed":
                code = str(result.get("errorCode") or "refresh_failed")
                if code in ("maintenance_busy", "operation_busy"):
                    self._error(HTTPStatus.CONFLICT, code)
                elif code == "invalid_country":
                    self._error(HTTPStatus.BAD_REQUEST, code)
                else:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "refresh_failed")
                return
            self._manager_result(result, HTTPStatus.ACCEPTED)
            return
        if self.command == "GET" and path == f"{API_PREFIX}/admin":
            self._manager_result(self.server.manager.managed_account_status())
            return
        if self.command == "PUT" and path == f"{API_PREFIX}/admin":
            payload = self._read_object({"username", "password"})
            username = str(payload.get("username") or "")
            password = str(payload.get("password") or "")
            result = self.server.manager.update_managed_account(username, password)
            if isinstance(result, dict) and result.get("ok") is False:
                self._manager_result(result)
            else:
                self._send_json(HTTPStatus.NO_CONTENT)
            return
        if self.command == "POST" and path == f"{API_PREFIX}/admin/verify":
            payload = self._read_object({"username", "password"})
            result = self.server.manager.verify_managed_account(
                str(payload.get("username") or ""),
                str(payload.get("password") or ""),
            )
            if isinstance(result, dict) and result.get("ok") is False:
                self._manager_result(result)
            else:
                self._send_json(HTTPStatus.NO_CONTENT)
            return
        if self.command == "POST" and path == f"{API_PREFIX}/admin/sessions":
            self._read_object(set())
            self._manager_result(
                self.server.manager.issue_managed_ui_session(), HTTPStatus.CREATED
            )
            return
        if self.command == "GET" and path == f"{API_PREFIX}/slots":
            self._manager_result(self.server.manager.managed_slots_snapshot())
            return
        if self.command == "POST" and path == f"{API_PREFIX}/slots":
            payload = self._read_object({"country", "proxyType", "candidateId"})
            country = str(payload.get("country") or "").strip().upper()
            proxy_type = str(payload.get("proxyType") or "").strip().lower()
            candidate_id = str(payload.get("candidateId") or "").strip()
            if not re.fullmatch(r"[A-Z]{2}", country) or proxy_type not in (
                "residential",
                "datacenter",
            ) or len(candidate_id) > 256:
                raise ValueError("invalid request")
            result = self.server.manager.create_managed_slot(country, proxy_type, candidate_id)
            self._manager_result(result, HTTPStatus.CREATED)
            return

        slot_route = self._slot_route(path)
        if slot_route:
            slot, action = slot_route
            if self.command == "GET" and not action:
                self._manager_result(self.server.manager.managed_slot_snapshot(slot))
                return
            if self.command == "POST" and action in ("rotate", "check", "assign"):
                payload = self._read_object({"candidateId", "country", "proxyType"} if action == "assign" else set())
                if action == "assign":
                    candidate_id = str(payload.get("candidateId") or "").strip()
                    country = str(payload.get("country") or "").strip().upper()
                    proxy_type = str(payload.get("proxyType") or "").strip().lower()
                    if not candidate_id or len(candidate_id) > 256 or not re.fullmatch(r"[A-Z]{2}", country) or proxy_type not in ("residential", "datacenter"):
                        raise ValueError("invalid request")
                    self._manager_result(self.server.manager.assign_managed_slot(slot, candidate_id, country, proxy_type))
                    return
                method = (
                    self.server.manager.rotate_managed_slot
                    if action == "rotate"
                    else self.server.manager.check_managed_slot
                )
                self._manager_result(method(slot))
                return
            if self.command == "DELETE" and not action:
                result = self.server.manager.delete_managed_slot(slot)
                if isinstance(result, dict) and result.get("ok") is False:
                    self._manager_result(result)
                else:
                    self._send_json(HTTPStatus.NO_CONTENT)
                return
        self._error(HTTPStatus.NOT_FOUND, "not_found")

    def _handle(self) -> None:
        try:
            self._dispatch()
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request")
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error")

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def create_server(manager: Any, address: tuple[str, int], token: str) -> ControlHTTPServer:
    host, port = address
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError as exc:
        raise ValueError("control address must use an IP loopback host") from exc
    if not loopback or not 0 <= int(port) <= 65535:
        raise ValueError("control address must be loopback")
    if not token.strip():
        raise ValueError("control token is required")
    return ControlHTTPServer((host, int(port)), manager, token.strip())


def start_control_server(
    manager: Any, address: str, token_file: Path, *, start: bool = True
) -> ControlHTTPServer:
    host, separator, raw_port = address.rpartition(":")
    if separator != ":" or not host:
        raise ValueError("invalid control address")
    try:
        port = int(raw_port)
        token = token_file.read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        raise ValueError("invalid control configuration") from exc
    server = create_server(manager, (host, port), token)
    if start:
        thread = threading.Thread(
            target=server.serve_forever, name="aimili-control-api", daemon=True
        )
        thread.start()
        server.control_thread = thread
    return server
