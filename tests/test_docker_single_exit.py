import contextlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "deploy" / "docker-single-exit"
ENTRYPOINT = DOCKER_DIR / "entrypoint.py"
COMPOSE_FILE = DOCKER_DIR / "compose.yaml"


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


class DockerComposeContractTests(unittest.TestCase):
    def compose_config(self):
        self.assertTrue(COMPOSE_FILE.is_file(), "Docker Compose file is missing")
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_effective_compose_confines_network_ports_and_privileges(self):
        config = self.compose_config()
        self.assertEqual(set(config["services"]), {"aimilivpn-single"})
        service = config["services"]["aimilivpn-single"]

        self.assertFalse(service.get("privileged", False))
        self.assertNotEqual(service.get("network_mode"), "host")
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["cap_add"], ["NET_ADMIN"])
        self.assertTrue(service["read_only"])
        self.assertEqual(service["devices"], [{"source": "/dev/net/tun", "target": "/dev/net/tun", "permissions": "rwm"}])
        self.assertIn("no-new-privileges:true", service["security_opt"])

        ports = {
            (item["host_ip"], int(item["published"]), item["target"], item["protocol"])
            for item in service["ports"]
        }
        self.assertEqual(
            ports,
            {
                ("127.0.0.1", 17928, 7928, "tcp"),
                ("127.0.0.1", 18787, 8787, "tcp"),
            },
        )
        self.assertEqual(set(service["networks"]), {"single-exit-net"})

    def test_effective_compose_uses_only_the_dedicated_data_volume(self):
        config = self.compose_config()
        service = config["services"]["aimilivpn-single"]
        self.assertEqual(set(config["volumes"]), {"single-exit-data"})
        self.assertEqual(
            service["volumes"],
            [
                {
                    "type": "volume",
                    "source": "single-exit-data",
                    "target": "/var/lib/aimilivpn",
                }
            ],
        )
        serialized = json.dumps(config, sort_keys=True)
        for forbidden in ("10808", "docker.sock", "network_mode\": \"host", "ssh ny"):
            self.assertNotIn(forbidden, serialized)

        environment = service["environment"]
        self.assertEqual(environment["MULTI_EXIT_SLOTS"], "0")
        self.assertEqual(environment["LOCAL_PROXY_HOST"], "0.0.0.0")
        self.assertEqual(environment["UI_HOST"], "0.0.0.0")
        self.assertEqual(environment["AIMILI_CONTROL_ADDRESS"], "127.0.0.1:8790")


if __name__ == "__main__":
    unittest.main()
