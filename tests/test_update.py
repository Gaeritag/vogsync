"""Exercise the updater without changing the user's installation."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vogsync-update-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.checkout = self.base / "source with spaces"
        self.checkout.mkdir()
        project = Path(__file__).resolve().parents[1]
        for name in ("update.sh", "install.sh", "vogsync.py"):
            shutil.copy2(project / name, self.checkout / name)
        self.home = self.base / "home with spaces"
        self.destination = self.home / ".local" / "bin" / "vogsync"
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("old executable\n")
        self.destination.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home))

    def run_update(self, *args):
        return subprocess.run(["sh", str(self.checkout / "update.sh"), *args],
                              cwd=self.base, env=self.env, capture_output=True, text=True)

    def test_update_from_another_directory(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.destination.read_bytes(), (self.checkout / "vogsync.py").read_bytes())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(self.destination.parent.glob(".vogsync-update.*")), [])

    def test_invalid_source_preserves_installation_and_cleans_up(self):
        (self.checkout / "vogsync.py").write_text("def invalid syntax\n")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_text(), "old executable\n")
        self.assertEqual(list(self.destination.parent.glob(".vogsync-update.*")), [])

    def test_missing_source_preserves_installation(self):
        (self.checkout / "vogsync.py").unlink()
        self.assertEqual(self.run_update().returncode, 1)
        self.assertEqual(self.destination.read_text(), "old executable\n")

    def test_requires_existing_installation(self):
        self.destination.unlink()
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("install.sh first", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_refuses_symlinks_and_directories(self):
        target = self.base / "unrelated"
        self.destination.rename(target)
        self.destination.symlink_to(target)
        self.assertEqual(self.run_update().returncode, 1)
        self.assertEqual(target.read_text(), "old executable\n")
        self.destination.unlink()
        self.destination.mkdir()
        self.assertEqual(self.run_update().returncode, 1)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_help_and_invalid_arguments(self):
        self.assertEqual(self.run_update("--help").returncode, 0)
        self.assertEqual(self.run_update("--unknown").returncode, 2)
        self.assertEqual(self.destination.read_text(), "old executable\n")

    def test_missing_home(self):
        self.env["HOME"] = ""
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("HOME is not set", result.stderr)

    def test_install_then_update(self):
        self.destination.unlink()
        result = subprocess.run(["sh", str(self.checkout / "install.sh")], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with (self.checkout / "vogsync.py").open("a") as handle:
            handle.write("\n# Updated source\n")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertTrue(self.destination.read_text().endswith("# Updated source\n"))


if __name__ == "__main__":
    unittest.main()
