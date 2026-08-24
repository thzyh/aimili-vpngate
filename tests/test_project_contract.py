import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProjectContractTests(unittest.TestCase):
    def test_readme_documents_pool_controls(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in (
            "TARGET_VALID_POOL_SIZE",
            "MAX_FETCH_ROWS",
            "NODE_TEST_BATCH_SIZE",
            "PROBE_FAILURE_COOLDOWN_SECONDS",
        ):
            self.assertIn(name, text)

    def test_selfcheck_rejects_visible_unavailable_nodes(self):
        text = (ROOT / "scripts" / "selfcheck_multiexit.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("probe_status", text)
        self.assertIn("unavailable", text)

    def test_installer_defaults_to_custom_fork_and_supports_pinned_ref(self):
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('DEFAULT_USER="thzyh"', installer)
        self.assertIn('DEFAULT_DEPLOY_BRANCH="custom"', installer)
        self.assertIn('DEPLOY_REF="${4:-}"', installer)
        self.assertIn(
            'DEPLOY_BRANCH="${REQUESTED_BRANCH:-$DEFAULT_DEPLOY_BRANCH}"',
            installer,
        )
        self.assertIn('git reset --hard "${DEPLOY_REF}"', installer)

    def test_readme_install_command_uses_custom_fork(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "raw.githubusercontent.com/thzyh/aimili-vpngate/custom/install.sh",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
