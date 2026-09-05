import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "deploy" / "docker-single-exit"
ENTRYPOINT = DOCKER_DIR / "entrypoint.py"


def load_entrypoint(test_case: unittest.TestCase):
    test_case.assertTrue(ENTRYPOINT.is_file(), "Docker entrypoint is missing")
    spec = importlib.util.spec_from_file_location("docker_single_exit_entrypoint", ENTRYPOINT)
    test_case.assertIsNotNone(spec)
    test_case.assertIsNotNone(spec.loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DockerEntrypointTests(unittest.TestCase):
    def test_first_run_creates_closed_runtime_files_without_printing_secrets(self):
        module = load_entrypoint(self)
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = pathlib.Path(temporary)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                module.ensure_runtime(data_dir)

            auth_path = data_dir / "ui_auth.json"
            token_path = data_dir / "control.token"
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
            token = token_path.read_text(encoding="utf-8").strip()

            self.assertGreaterEqual(len(auth["username"]), 12)
            self.assertGreaterEqual(len(auth["password"]), 32)
            self.assertGreaterEqual(len(auth["secret_path"]), 24)
            self.assertEqual(auth["host"], "0.0.0.0")
            self.assertEqual(auth["port"], 8787)
            self.assertEqual(auth["proxy_port"], 7928)
            self.assertTrue(auth["connection_enabled"])
            self.assertGreaterEqual(len(token), 43)
            self.assertNotIn(auth["username"], output.getvalue())
            self.assertNotIn(auth["password"], output.getvalue())
            self.assertNotIn(auth["secret_path"], output.getvalue())
            self.assertNotIn(token, output.getvalue())

            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(auth_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

    def test_second_run_preserves_existing_credentials_byte_for_byte(self):
        module = load_entrypoint(self)
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = pathlib.Path(temporary)
            module.ensure_runtime(data_dir)
            before_auth = (data_dir / "ui_auth.json").read_bytes()
            before_token = (data_dir / "control.token").read_bytes()

            module.ensure_runtime(data_dir)

            self.assertEqual((data_dir / "ui_auth.json").read_bytes(), before_auth)
            self.assertEqual((data_dir / "control.token").read_bytes(), before_token)

    def test_invalid_existing_auth_is_rejected_without_overwrite(self):
        module = load_entrypoint(self)
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = pathlib.Path(temporary)
            auth_path = data_dir / "ui_auth.json"
            original = b'{"username":"incomplete"}\n'
            auth_path.write_bytes(original)

            with self.assertRaisesRegex(ValueError, "invalid existing UI configuration"):
                module.ensure_runtime(data_dir)

            self.assertEqual(auth_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
