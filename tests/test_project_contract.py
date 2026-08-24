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


if __name__ == "__main__":
    unittest.main()
