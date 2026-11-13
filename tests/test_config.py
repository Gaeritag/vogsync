"""Regression tests for the repository's remote configuration."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import vogsync


class ConfigTests(unittest.TestCase):
    def test_remote_set_leaves_config_available_for_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "libft").mkdir()
            args = vogsync.build_parser().parse_args(["remote", "set", "libft", "git@example:libft"])

            vogsync.command_remote(args, root)

            self.assertFalse((root / ".gitignore").exists())
            self.assertEqual(vogsync.load_config(root), {"libft": "git@example:libft"})
            status = subprocess.run(["git", "status", "--short", "--untracked-files=all"],
                                    cwd=root, check=True, capture_output=True, text=True)
            self.assertIn("?? .vogsync.json", status.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
