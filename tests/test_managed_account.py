import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import vpngate_manager as manager


class ManagedAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.auth_file = self.data_dir / "ui_auth.json"
        self.original_data_dir = manager.DATA_DIR
        manager.DATA_DIR = self.data_dir
        self.addCleanup(setattr, manager, "DATA_DIR", self.original_data_dir)
        manager.active_sessions.clear()
        self.addCleanup(manager.active_sessions.clear)
        self.auth_file.write_text(
            json.dumps(
                {
                    "username": "owner",
                    "password": "old-password-marker",
                    "secret_path": "private-path-marker",
                    "port": 8787,
                    "proxy_port": 7928,
                    "routing_mode": "auto",
                }
            ),
            encoding="utf-8",
        )

    def test_status_exposes_username_without_password_or_private_path(self):
        status = manager.managed_account_status()
        self.assertEqual(status, {"username": "owner", "totpSupported": False})
        serialized = json.dumps(status)
        self.assertNotIn("old-password-marker", serialized)
        self.assertNotIn("private-path-marker", serialized)

    def test_update_atomically_preserves_other_settings_and_revokes_sessions(self):
        manager.active_sessions["old-session-marker"] = 1_700_000_900
        with mock.patch.object(os, "replace", wraps=os.replace) as replace:
            result = manager.update_managed_account("renamed", "new-password-marker")

        self.assertEqual(result, {"ok": True})
        replace.assert_called_once()
        saved = json.loads(self.auth_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["username"], "renamed")
        self.assertEqual(saved["password"], "new-password-marker")
        self.assertEqual(saved["secret_path"], "private-path-marker")
        self.assertEqual(saved["port"], 8787)
        self.assertEqual(manager.active_sessions, {})
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.data_dir.iterdir()))

    def test_update_rejects_invalid_credentials_without_changing_file(self):
        before = self.auth_file.read_bytes()
        for username, password in (("", "new-password-marker"), ("owner", "short"), ("bad\nname", "new-password-marker")):
            with self.subTest(username=username, password_length=len(password)):
                result = manager.update_managed_account(username, password)
                self.assertEqual(result, {"ok": False, "error_code": "invalid_credentials"})
                self.assertEqual(self.auth_file.read_bytes(), before)

    def test_verify_compares_both_fields_without_returning_them(self):
        self.assertEqual(
            manager.verify_managed_account("owner", "old-password-marker"),
            {"ok": True},
        )
        self.assertEqual(
            manager.verify_managed_account("owner", "wrong-password-marker"),
            {"ok": False, "error_code": "credentials_rejected"},
        )

    def test_issued_session_is_opaque_and_expires_within_five_minutes(self):
        with mock.patch.object(manager.time, "time", return_value=1_700_000_000):
            result = manager.issue_managed_ui_session()

        self.assertEqual(result["cookieName"], "session")
        self.assertEqual(result["expiresAt"], 1_700_000_300)
        self.assertGreaterEqual(len(result["sessionToken"]), 32)
        self.assertEqual(manager.active_sessions[result["sessionToken"]], 1_700_000_300)
        self.assertNotIn("secret_path", result)
        self.assertNotIn("password", result)


if __name__ == "__main__":
    unittest.main()
