"""Exercise the cross-platform installer without changing the user's files."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
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
        for name in ("install.py", "vogsync.py"):
            shutil.copy2(project / name, self.checkout / name)
        self.home = self.base / "home with spaces"
        self.destination = self.home / ("vogsync" if os.name == "nt" else ".local") / "bin" / ("vogsync.py" if os.name == "nt" else "vogsync")
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("old executable\n")
        self.destination.chmod(0o755)
        if os.name == "nt":
            (self.destination.parent / "vogsync.bat").write_text("old launcher\n")
        self.env = dict(os.environ, HOME=str(self.home), LOCALAPPDATA=str(self.home))

    def run_update(self, *args):
        return subprocess.run([sys.executable, str(self.checkout / "install.py"), "update", *args],
                              cwd=self.base, env=self.env, capture_output=True, text=True)

    def test_update_from_another_directory(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.destination.read_bytes(), (self.checkout / "vogsync.py").read_bytes())
        if os.name != "nt":
            self.assertEqual(self.destination.stat().st_mode & 0o777, 0o755)
        self.assertEqual(list(self.destination.parent.glob(".vogsync-update-*")), [])

    def test_invalid_source_preserves_installation_and_cleans_up(self):
        (self.checkout / "vogsync.py").write_text("def invalid syntax\n")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_text(), "old executable\n")
        self.assertEqual(list(self.destination.parent.glob(".vogsync-update-*")), [])

    def test_missing_source_preserves_installation(self):
        (self.checkout / "vogsync.py").unlink()
        self.assertEqual(self.run_update().returncode, 1)
        self.assertEqual(self.destination.read_text(), "old executable\n")

    def test_requires_existing_installation(self):
        self.destination.unlink()
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("install.py install first", result.stderr)
        self.assertFalse(self.destination.exists())

    def test_refuses_symlinks_and_directories(self):
        target = self.base / "unrelated"
        self.destination.rename(target)
        try:
            self.destination.symlink_to(target)
        except OSError:
            if os.name == "nt":
                self.skipTest("Windows symlinks require Developer Mode or elevated privileges")
            raise
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
        variable = "LOCALAPPDATA" if os.name == "nt" else "HOME"
        self.env[variable] = ""
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn(variable + " is not set", result.stderr)

    def test_install_then_update(self):
        self.destination.unlink()
        if os.name == "nt":
            (self.destination.parent / "vogsync.bat").unlink()
        result = subprocess.run([sys.executable, str(self.checkout / "install.py")], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        if os.name != "nt":
            result = subprocess.run([str(self.destination), "--version"], env=self.env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("vogsync", result.stdout)
        with (self.checkout / "vogsync.py").open("a") as handle:
            handle.write("\n# Updated source\n")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertTrue(self.destination.read_text().endswith("# Updated source\n"))

    def test_install_refuses_existing_files(self):
        result = subprocess.run([sys.executable, str(self.checkout / "install.py"), "install"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.destination.read_text(), "old executable\n")

    def test_missing_git_preserves_installation(self):
        self.env["PATH"] = ""
        result = self.run_update()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Git is required", result.stderr)
        self.assertEqual(self.destination.read_text(), "old executable\n")

    def test_top_level_help_and_invalid_mode(self):
        command = [sys.executable, str(self.checkout / "install.py")]
        result = subprocess.run(command + ["--help"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Linux/macOS", result.stdout)
        self.assertIn("Windows", result.stdout)
        result = subprocess.run(command + ["unknown"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
