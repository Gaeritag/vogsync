"""Doctor integration tests using disposable, offline Git repositories."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vogsync


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vogsync-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "monorepo"
        self.root.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.base),
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0")
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Doctor Test")
        self.git("config", "user.email", "doctor@example.invalid")
        (self.root / "libft").mkdir()
        (self.root / "libft" / "Makefile").write_text("all:\n\t@true\n")
        (self.root / ".gitignore").write_text("/.vogsync.json\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial projects")
        self.destination = self.base / "libft.git"
        subprocess.run(["git", "init", "--bare", str(self.destination)], check=True, capture_output=True)
        self.configure({"libft": str(self.destination)})

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout

    def configure(self, config):
        path = self.root / vogsync.CONFIG_FILE_NAME
        path.write_text(json.dumps(config))
        path.chmod(0o600)

    def diagnose(self, remote=False, branch="master"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            healthy = vogsync.command_doctor(self.root, remote, branch)
        return healthy, output.getvalue()

    def test_healthy_repository_and_no_network_by_default(self):
        with patch.object(vogsync, "remote_head", side_effect=AssertionError("Network used")):
            healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("0 error(s)", output)
        self.assertIn("Git object integrity", output)
        self.assertIn("committed project tree exists", output)

    def test_read_only(self):
        def snapshot():
            return {str(path.relative_to(self.root)): path.read_bytes()
                    for path in self.root.rglob("*") if path.is_file()}
        before = snapshot()
        self.diagnose()
        self.assertEqual(before, snapshot())

    def test_missing_project_and_invalid_config_continue_checks(self):
        self.configure({"missing": str(self.destination)})
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("existing, non-symlink directory", output)
        self.assertIn("Diagnosis:", output)
        (self.root / vogsync.CONFIG_FILE_NAME).write_text("{bad json")
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("Could not read", output)
        self.assertIn("No main repo Git remote", output)

    def test_nested_git_directory_and_file(self):
        nested = self.root / "libft" / "nested"
        nested.mkdir()
        (nested / ".git").mkdir()
        linked = self.root / "linked"
        linked.mkdir()
        (linked / ".git").write_text("gitdir: /nonexistent\n")
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("Nested Git repository at libft/nested", output)
        self.assertIn("Nested Git repository at linked", output)

    def test_gitlink_detected_even_without_git_marker(self):
        sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-index", "--add", "--cacheinfo", "160000,{},submodule".format(sha))
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("Submodule/gitlink at submodule", output)

    def test_dirty_tree_is_warning(self):
        (self.root / "libft" / "Makefile").write_text("changed\n")
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("[WARN] Uncommitted changes", output)

    def test_unborn_branch(self):
        self.git("checkout", "--orphan", "empty")
        self.git("rm", "-rf", ".")
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("initial commit", output)
        self.assertIn("Diagnosis:", output)

    def test_detached_head(self):
        self.git("checkout", "--detach")
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("[WARN] Detached HEAD", output)

    def test_upstream_comparison_and_operation_in_progress(self):
        upstream = self.base / "upstream.git"
        subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True)
        self.git("remote", "add", "origin", str(upstream))
        self.git("push", "-u", "origin", "main")
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("0 ahead, 0 behind", output)
        self.git("commit", "--allow-empty", "-m", "Ahead")
        (self.root / ".git" / "CHERRY_PICK_HEAD").write_text(self.git("rev-parse", "HEAD"))
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("1 ahead, 0 behind", output)
        self.assertIn("Git operation in progress (CHERRY_PICK_HEAD)", output)

    def test_project_symlink_and_config_symlink(self):
        try:
            (self.root / "alias").symlink_to(self.root / "libft", target_is_directory=True)
        except OSError:
            if os.name == "nt":
                self.skipTest("Windows symlinks require Developer Mode or elevated privileges")
            raise
        self.configure({"alias": str(self.destination)})
        config = self.root / vogsync.CONFIG_FILE_NAME
        saved = self.base / "config.json"
        config.rename(saved)
        config.symlink_to(saved)
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn(".vogsync.json is a symlink", output)
        self.assertIn("non-symlink directory", output)

    def test_subtree_unavailable(self):
        original = subprocess.run

        def without_subtree(args, **kwargs):
            if args[:3] == ["git", "subtree", "-h"]:
                return subprocess.CompletedProcess(args, 1, "", "not a git command")
            return original(args, **kwargs)

        with patch.object(subprocess, "run", side_effect=without_subtree):
            healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("git subtree is unavailable", output)
        self.assertIn("Diagnosis:", output)

    def test_tracked_config_is_error_even_if_ignored(self):
        self.git("add", "--force", vogsync.CONFIG_FILE_NAME)
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("is tracked", output)

    def test_config_permissions_and_ignore_rule(self):
        (self.root / ".gitignore").write_text("")
        (self.root / vogsync.CONFIG_FILE_NAME).chmod(0o644)
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("is not ignored", output)
        if os.name == "nt":
            self.assertNotIn("chmod 600", output)
        else:
            self.assertIn("chmod 600", output)

    def test_duplicate_destinations(self):
        (self.root / "ft_printf").mkdir()
        (self.root / "ft_printf" / "Makefile").write_text("all:\n")
        self.git("add", ".")
        self.git("commit", "-m", "Second project")
        self.configure({"libft": "git@example.invalid:42/project.git",
                        "ft_printf": "https://example.invalid/42/project/"})
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("share a deployment destination", output)

    def test_deploying_to_monorepo_push_url(self):
        self.git("remote", "add", "origin", "https://example.invalid/42/cursus.git")
        self.git("remote", "set-url", "--push", "origin", "git@example.invalid:42/push.git")
        self.configure({"libft": "https://example.invalid/42/push"})
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("deployment points to the main repo", output)

    def test_git_url_rewrites_and_local_self_destination(self):
        self.git("remote", "add", "origin", "https://example.invalid/42/cursus.git")
        self.git("config", "url.https://example.invalid/42/.insteadOf", "school:")
        self.configure({"libft": "school:cursus.git"})
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("deployment points to the main repo", output)
        for url in (str(self.root), str(self.root / ".git"), "."):
            self.configure({"libft": url})
            healthy, output = self.diagnose()
            self.assertFalse(healthy)
            self.assertIn("deployment points to the main repo", output)

    def test_identity_values_are_displayed(self):
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("Git user.name is configured: Doctor Test", output)
        self.assertIn("Git user.email is configured: doctor@example.invalid", output)

    def test_projects_without_urls_are_fully_checked(self):
        self.configure({})
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("libft: regular top-level project directory", output)
        self.assertIn("libft: committed project tree exists", output)
        self.assertIn("libft: no project Vogsphere URL configured", output)
        self.assertIn("separate from main repo remotes", output)

    def test_new_project_without_url_is_checked(self):
        (self.root / "new").mkdir()
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("new: regular top-level project directory", output)
        self.assertIn("HEAD:new", output)

    def test_excluded_directories_and_remote_set_preservation(self):
        other = self.root / "other"
        other.mkdir()
        (other / "notes.txt").write_text("notes\n")
        self.git("add", "other")
        self.git("commit", "-m", "Notes")
        self.configure({"exclude": ["other"]})
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("other: excluded from project diagnosis", output)
        self.assertNotIn("other: no project Vogsphere URL", output)
        self.assertNotIn("other: committed project tree", output)
        args = vogsync.build_parser().parse_args(["remote", "set", "libft", str(self.destination)])
        with contextlib.redirect_stdout(io.StringIO()):
            vogsync.command_remote(args, self.root)
        settings = json.loads((self.root / vogsync.CONFIG_FILE_NAME).read_text())
        self.assertEqual(settings["exclude"], ["other"])
        self.assertEqual(vogsync.load_config(self.root), {"libft": str(self.destination)})

    def test_invalid_exclusions(self):
        for excluded in ("other", [1], ["../other"], [".git"], ["libft"]):
            with self.subTest(excluded=excluded):
                self.configure({"libft": str(self.destination), "exclude": excluded})
                healthy, output = self.diagnose()
                self.assertFalse(healthy)
                self.assertIn("Diagnosis:", output)

    def test_remote_set_rejects_excluded_and_reserved_projects(self):
        self.configure({"exclude": ["libft"]})
        before = (self.root / vogsync.CONFIG_FILE_NAME).read_bytes()
        for project in ("libft", "exclude"):
            args = vogsync.build_parser().parse_args(["remote", "set", project, str(self.destination)])
            with self.assertRaises(vogsync.ToolError):
                vogsync.command_remote(args, self.root)
        self.assertEqual(before, (self.root / vogsync.CONFIG_FILE_NAME).read_bytes())

    def test_ignored_directory_is_not_discovered(self):
        (self.root / "build").mkdir()
        with (self.root / ".gitignore").open("a") as handle:
            handle.write("/build/\n")
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertNotIn("build:", output)

    def test_uncommitted_project(self):
        (self.root / "new").mkdir()
        self.configure({"new": str(self.destination)})
        healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("new:", output)
        self.assertIn("HEAD:new", output)

    def test_remote_access_to_local_bare_repository(self):
        healthy, output = self.diagnose(remote=True, branch="submission")
        self.assertTrue(healthy, output)
        self.assertIn("branch submission does not exist yet", output)
        self.git("push", str(self.destination), "HEAD:refs/heads/submission")
        healthy, output = self.diagnose(remote=True, branch="submission")
        self.assertTrue(healthy, output)
        self.assertIn("branch submission exists", output)

    def test_remote_failure_does_not_hide_other_checks(self):
        with patch.object(vogsync, "remote_head", side_effect=vogsync.ToolError("Remote check timed out")):
            healthy, output = self.diagnose(remote=True)
        self.assertFalse(healthy)
        self.assertIn("Remote check timed out", output)
        self.assertIn("Diagnosis:", output)

    def test_fsck_failure_does_not_hide_config_errors(self):
        original = vogsync.git

        def fail_fsck(args, root=None):
            if "fsck" in args:
                raise vogsync.ToolError("Corrupt object")
            return original(args, root)

        self.configure({"missing": str(self.destination)})
        with patch.object(vogsync, "git", side_effect=fail_fsck):
            healthy, output = self.diagnose()
        self.assertFalse(healthy)
        self.assertIn("Corrupt object", output)
        self.assertIn("existing, non-symlink directory", output)

    def test_worktree_support(self):
        linked = self.base / "worktree"
        self.git("worktree", "add", "-b", "linked", str(linked))
        self.root = linked
        self.configure({"libft": str(self.destination)})
        healthy, output = self.diagnose()
        self.assertTrue(healthy, output)
        self.assertIn("No nested Git repositories", output)

    def test_cli_exit_codes_and_subdirectory(self):
        script = str(Path(vogsync.__file__).resolve())
        command = [sys.executable, script, "doctor"]
        result = subprocess.run(command, cwd=self.root / "libft", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.configure({"missing": str(self.destination)})
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        result = subprocess.run(command + ["--branch", "bad..branch"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        result = subprocess.run(command, cwd=self.base, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
