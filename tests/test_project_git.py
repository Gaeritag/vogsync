"""Project-scoped Git commands do not touch neighboring projects."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "vogsync.py"


class ProjectGitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vogsync-project-git-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        for project in ("libft", "ft_printf"):
            (self.root / project).mkdir()
            (self.root / project / "code.c").write_text("initial\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial projects")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True, check=True).stdout

    def vogsync(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.root,
                              env=self.env, capture_output=True, text=True)

    def test_status_project_filters_changes_and_remotes(self):
        for project in ("libft", "ft_printf"):
            (self.root / project / "code.c").write_text("changed\n")
        (self.root / ".vogsync.json").write_text('{"libft": "libft-url", "ft_printf": "other-url"}')
        result = self.vogsync("status", "libft")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("libft/code.c", result.stdout)
        self.assertIn("libft-url", result.stdout)
        self.assertNotIn("ft_printf/code.c", result.stdout)
        self.assertNotIn("other-url", result.stdout)
        self.assertIn("ft_printf/code.c", self.vogsync("status").stdout)

    def test_scoped_add_diff_log_restore(self):
        for project in ("libft", "ft_printf"):
            (self.root / project / "code.c").write_text("changed\n")
        self.assertIn("+changed", self.vogsync("diff", "libft").stdout)
        self.assertNotIn("ft_printf", self.vogsync("diff", "libft").stdout)
        result = self.vogsync("add", "libft")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "libft/code.c")
        self.assertIn("+changed", self.vogsync("diff", "libft", "--cached").stdout)
        self.assertIn("Initial projects", self.vogsync("log", "libft").stdout)
        self.assertEqual(self.vogsync("restore", "libft", "--staged").returncode, 0)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertEqual(self.vogsync("restore", "libft").returncode, 0)
        self.assertEqual((self.root / "libft" / "code.c").read_text(), "initial\n")
        self.assertEqual((self.root / "ft_printf" / "code.c").read_text(), "changed\n")

    def test_remote_status_only_checks_selected_project(self):
        destination = self.root / "remote.git"
        self.git("init", "--bare", str(destination))
        (self.root / ".vogsync.json").write_text(
            '{{"libft": "{}", "ft_printf": "missing-remote.git"}}'.format(destination.as_posix()))
        result = self.vogsync("status", "libft", "--remote")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("libft: remote master does not exist", result.stdout)
        self.assertNotIn("ft_printf:", result.stdout)

    def test_missing_remote_does_not_prevent_local_status(self):
        (self.root / "libft" / "code.c").write_text("changed\n")
        result = self.vogsync("status", "libft", "--remote")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("libft/code.c", result.stdout)
        self.assertIn("No project remote configured for libft", result.stdout)

    def test_defaults_to_current_project_and_rejects_invalid_names(self):
        (self.root / "libft" / "new.c").write_text("new\n")
        self.assertEqual(self.vogsync("add", cwd=self.root / "libft").returncode, 0)
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "libft/new.c")
        self.assertEqual(self.vogsync("add").returncode, 1)
        for name in ("../libft", ".git", "missing"):
            for command in ("status", "add", "diff", "log", "restore"):
                result = self.vogsync(command, name)
                self.assertEqual(result.returncode, 1, (command, name, result.stderr))


if __name__ == "__main__":
    unittest.main()
