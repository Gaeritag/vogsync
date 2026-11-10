"""Exercise two-destination deployments with disposable local Git repositories."""

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


class DeployTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vogsync-deploy-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "main repo"
        self.root.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.base),
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0")
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Deploy Test")
        self.git("config", "user.email", "deploy@example.invalid")
        (self.root / "libft").mkdir()
        (self.root / "libft" / "code.c").write_text("initial\n")
        (self.root / ".gitignore").write_text("/.vogsync.json\n")
        self.git("add", ".")
        self.git("commit", "-m", "Initial projects")
        self.github = self.base / "github.git"
        self.vogsphere = self.base / "vogsphere.git"
        for destination in (self.github, self.vogsphere):
            self.git("init", "--bare", str(destination))
        self.git("remote", "add", "origin", str(self.github))
        self.git("push", "-u", "origin", "main")
        self.initial = self.git("rev-parse", "HEAD")
        self.configure(self.vogsphere)
        (self.root / "libft" / "code.c").write_text("updated\n")
        (self.root / "README.md").write_text("whole monorepo\n")
        self.git("add", ".")
        self.git("commit", "-m", "Update project and main repo")

    def git(self, *args, root=None):
        return subprocess.run(["git", *args], cwd=root or self.root, check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def configure(self, destination):
        (self.root / ".vogsync.json").write_text(json.dumps({"libft": str(destination)}))

    def deploy(self, *options):
        args = vogsync.build_parser().parse_args(["deploy", "libft", *options])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            vogsync.command_deploy(args, self.root)
        return output.getvalue()

    def assert_not_deployed(self):
        self.assertEqual(self.git("ls-remote", str(self.vogsphere), "refs/heads/master"), "")

    def test_deploy_pushes_whole_monorepo_then_project(self):
        original = vogsync.git
        pushes = []

        def record(args, root=None):
            if args[0] == "push":
                pushes.append(args)
            return original(args, root)

        with patch.object(vogsync, "git", side_effect=record):
            output = self.deploy()
        self.assertEqual(pushes[0], ["push", "--", "origin", "HEAD:refs/heads/main"])
        self.assertEqual(pushes[1][:4], ["push", "--force", "--", str(self.vogsphere)])
        self.assertEqual(self.git("rev-parse", "main", root=self.github), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("show", "main:README.md", root=self.github), "whole monorepo")
        self.assertEqual(self.git("ls-tree", "--name-only", "master", root=self.vogsphere), "code.c")
        self.assertEqual(self.git("show", "master:code.c", root=self.vogsphere), "updated")
        self.assertIn("Main repo pushed to origin/main", output)

    def test_dry_run_does_not_push_either_destination(self):
        self.deploy("--dry-run")
        self.assertEqual(self.git("rev-parse", "main", root=self.github), self.initial)
        self.assert_not_deployed()

    def test_cli_deploy_and_removed_command_name(self):
        command = [sys.executable, str(Path(vogsync.__file__).resolve())]
        result = subprocess.run(command + ["deploy", "libft", "--dry-run"], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run successful", result.stdout)
        self.assertEqual(self.git("rev-parse", "main", root=self.github), self.initial)
        self.assert_not_deployed()
        result = subprocess.run(command + ["render", "libft"], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_missing_upstream_stops_before_deployment(self):
        self.git("branch", "--unset-upstream")
        with self.assertRaisesRegex(vogsync.ToolError, "no upstream"):
            self.deploy()
        self.assert_not_deployed()

    def test_detached_head_stops_before_deployment(self):
        self.git("checkout", "--detach")
        with self.assertRaisesRegex(vogsync.ToolError, "detached"):
            self.deploy()
        self.assert_not_deployed()

    def test_rejected_main_repo_push_stops_vogsphere_push(self):
        # Advance GitHub independently so the main repo push is non-fast-forward.
        other = self.base / "other clone"
        self.git("clone", "--branch", "main", str(self.github), str(other))
        self.git("config", "user.name", "Other", root=other)
        self.git("config", "user.email", "other@example.invalid", root=other)
        (other / "remote.txt").write_text("remote commit\n")
        self.git("add", ".", root=other)
        self.git("commit", "-m", "Independent change", root=other)
        self.git("push", root=other)
        remote_head = self.git("rev-parse", "HEAD", root=other)
        with self.assertRaisesRegex(vogsync.ToolError, "Main repo push failed; Vogsphere was not updated"):
            self.deploy()
        self.assertEqual(self.git("rev-parse", "main", root=self.github), remote_head)
        self.assert_not_deployed()

    def test_vogsphere_failure_reports_that_main_repo_was_pushed(self):
        self.configure(self.base / "missing-vogsphere.git")
        with self.assertRaisesRegex(vogsync.ToolError, "Main repo push succeeded, but Vogsphere deployment failed"):
            self.deploy()
        self.assertEqual(self.git("rev-parse", "main", root=self.github), self.git("rev-parse", "HEAD"))

    def test_uses_configured_upstream_and_requested_project_branch(self):
        self.git("remote", "rename", "origin", "github")
        self.git("config", "push.default", "nothing")
        self.deploy("--branch", "submission")
        self.assertEqual(self.git("rev-parse", "main", root=self.github), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("show", "submission:code.c", root=self.vogsphere), "updated")
        self.assert_not_deployed()

    def test_dirty_tree_stops_both_pushes(self):
        (self.root / "libft" / "code.c").write_text("not committed\n")
        with self.assertRaisesRegex(vogsync.ToolError, "dirty"):
            self.deploy()
        self.assertEqual(self.git("rev-parse", "main", root=self.github), self.initial)
        self.assert_not_deployed()

    def test_pull_then_deploy_updates_both_destinations(self):
        # Publish the new commit, then test pulling from the old tip in this
        # disposable clone. No user repository or network is involved.
        self.git("push")
        latest = self.git("rev-parse", "HEAD")
        self.git("checkout", "-B", "main", self.initial)
        self.deploy("--pull")
        self.assertEqual(self.git("rev-parse", "HEAD"), latest)
        self.assertEqual(self.git("rev-parse", "main", root=self.github), latest)
        self.assertEqual(self.git("show", "master:code.c", root=self.vogsphere), "updated")


if __name__ == "__main__":
    unittest.main()
