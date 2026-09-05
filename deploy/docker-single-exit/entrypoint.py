#!/usr/bin/env python3
"""Prepare an isolated local runtime without exposing generated secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
from typing import NoReturn


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _valid_auth(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        isinstance(value.get("username"), str)
        and len(value["username"]) >= 1
        and isinstance(value.get("password"), str)
        and len(value["password"]) >= 12
        and isinstance(value.get("secret_path"), str)
        and len(value["secret_path"]) >= 16
        and value.get("host") == "0.0.0.0"
        and value.get("port") == 8787
        and value.get("proxy_port") == 7928
        and isinstance(value.get("connection_enabled"), bool)
    )


def ensure_runtime(data_dir: Path) -> None:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise ValueError("runtime data path must be a directory")
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass

    auth_path = data_dir / "ui_auth.json"
    if auth_path.exists():
        if auth_path.is_symlink() or not auth_path.is_file():
            raise ValueError("invalid existing UI configuration")
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid existing UI configuration") from exc
        if not _valid_auth(auth):
            raise ValueError("invalid existing UI configuration")
        try:
            os.chmod(auth_path, 0o600)
        except OSError:
            pass
    else:
        auth = {
            "username": secrets.token_urlsafe(12),
            "password": secrets.token_urlsafe(32),
            "secret_path": secrets.token_urlsafe(24),
            "host": "0.0.0.0",
            "port": 8787,
            "proxy_port": 7928,
            "routing_mode": "auto",
            "force_country": "",
            "routing_ip_type": "all",
            "connection_enabled": True,
            "fixed_node_id": "",
            "favorite_node_ids": [],
            "fav_fail_fallback": True,
        }
        payload = (json.dumps(auth, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _atomic_write(auth_path, payload)

    token_path = data_dir / "control.token"
    if token_path.exists():
        if token_path.is_symlink() or not token_path.is_file():
            raise ValueError("invalid existing control token")
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError("invalid existing control token") from exc
        if len(token) < 32:
            raise ValueError("invalid existing control token")
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
    else:
        _atomic_write(token_path, (secrets.token_urlsafe(32) + "\n").encode("utf-8"))


def main() -> NoReturn:
    data_dir = Path(os.environ.get("VPNGATE_DATA_DIR", "/var/lib/aimilivpn"))
    ensure_runtime(data_dir)
    os.execvp("python3", ["python3", "/opt/aimilivpn/vpngate_manager.py"])


if __name__ == "__main__":
    main()
