"""Windows installer logic, path regressions, and native launcher smoke tests."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vogsync


class WindowsInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vogsync-windows-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.checkout = self.base / "source with spaces"
        self.checkout.mkdir()
        project = Path(__file__).resolve().parents[1]
        for name in ("install.py", "vogsync.py"):
            shutil.copy2(project / name, self.checkout / name)
        self.local = self.base / "local app data"
        self.bin = self.local / "vogsync" / "bin"
        self.env = dict(os.environ, LOCALAPPDATA=str(self.local))

    def run_installer(self, mode="install", *args):
        # Exercise the Windows installation layout even on POSIX test hosts.
        code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "import install; install.WINDOWS = True; sys.exit(install.main())")
        return subprocess.run([sys.executable, "-c", code, str(self.checkout), mode, *args],
                              cwd=self.base, env=self.env, capture_output=True, text=True)

    def test_install_then_update(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.bin / "vogsync.py").read_bytes(), (self.checkout / "vogsync.py").read_bytes())
        self.assertIn('"%~dp0vogsync.py" %*', (self.bin / "vogsync.bat").read_text())
        with (self.checkout / "vogsync.py").open("a") as handle:
            handle.write("\n# updated\n")
        result = self.run_installer("update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.bin / "vogsync.py").read_text().endswith("# updated\n"))
        self.assertEqual(list(self.bin.glob(".vogsync-update-*")), [])

    def test_install_refuses_existing_file_without_partial_install(self):
        self.bin.mkdir(parents=True)
        (self.bin / "vogsync.bat").write_text("existing")
        self.assertEqual(self.run_installer().returncode, 1)
        self.assertEqual((self.bin / "vogsync.bat").read_text(), "existing")
        self.assertFalse((self.bin / "vogsync.py").exists())

    def test_update_requires_installation(self):
        result = self.run_installer("update")
        self.assertEqual(result.returncode, 1)
        self.assertIn("install.py install first", result.stderr)

    def test_invalid_source_preserves_installation(self):
        self.assertEqual(self.run_installer().returncode, 0)
        before = {p.name: p.read_bytes() for p in self.bin.iterdir()}
        (self.checkout / "vogsync.py").write_text("invalid python syntax\n")
        self.assertEqual(self.run_installer("update").returncode, 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.bin.iterdir()})

    def test_missing_source_and_localappdata(self):
        (self.checkout / "vogsync.py").unlink()
        self.assertEqual(self.run_installer().returncode, 1)
        self.env["LOCALAPPDATA"] = ""
        self.assertIn("LOCALAPPDATA is not set", self.run_installer().stderr)

    def test_help_and_unknown_arguments(self):
        self.assertEqual(self.run_installer("install", "--help").returncode, 0)
        self.assertEqual(self.run_installer("update", "--unknown").returncode, 2)
        self.assertFalse(self.bin.exists())

    def test_update_refuses_directory(self):
        self.assertEqual(self.run_installer().returncode, 0)
        path = self.bin / "vogsync.py"
        path.unlink()
        path.mkdir()
        self.assertEqual(self.run_installer("update").returncode, 1)
        self.assertTrue(path.is_dir())

    def test_update_refuses_symlink(self):
        self.assertEqual(self.run_installer().returncode, 0)
        path = self.bin / "vogsync.py"
        target = self.base / "unrelated.py"
        path.rename(target)
        before = target.read_bytes()
        try:
            path.symlink_to(target)
        except OSError:
            if os.name == "nt":
                self.skipTest("Windows symlinks require Developer Mode or elevated privileges")
            raise
        self.assertEqual(self.run_installer("update").returncode, 1)
        self.assertEqual(target.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Requires native cmd.exe")
    def test_native_windows_install_launcher_and_update(self):
        def batch(path, *args):
            command = subprocess.list2cmdline([str(path), *args])
            return subprocess.run(["cmd.exe", "/d", "/s", "/c", '"' + command + '"'],
                                  cwd=self.base, env=self.env, capture_output=True, text=True)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        result = batch(self.bin / "vogsync.bat", "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vogsync", result.stdout)
        self.assertEqual(batch(self.bin / "vogsync.bat", "--invalid").returncode, 2)
        self.assertEqual(self.run_installer().returncode, 1)
        self.assertEqual(self.run_installer("update").returncode, 0)


class WindowsPathTests(unittest.TestCase):
    def key(self, url):
        with patch.object(vogsync, "git", return_value=subprocess.CompletedProcess([], 0, url + "\n")):
            return vogsync.destination_key(Path.cwd(), url)

    def test_drive_paths_are_not_ssh_hosts(self):
        self.assertEqual(self.key("C:/Users/Student/repo.git"), self.key(r"c:\users\student\repo.git"))
        self.assertEqual(self.key("file:///C:/Users/Student/repo.git"), self.key("C:/Users/Student/repo.git"))

    def test_unc_and_encoded_file_urls(self):
        self.assertEqual(self.key("file://server/share/my%20repo.git"), self.key(r"\\server\share\my repo.git"))

    def test_drive_relative_project_rejected(self):
        with self.assertRaises(vogsync.ToolError):
            vogsync.validate_project("C:libft")


if __name__ == "__main__":
    unittest.main()
