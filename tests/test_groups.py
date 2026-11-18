"""Group workflows use only disposable repositories and local bare remotes."""

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


class GroupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vogsync-groups-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(HOME=str(self.base), XDG_CONFIG_HOME=str(self.base),
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0")
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.base / "main repo"
        self.source = self.base / "source"
        self.group = self.root / "push_swap"
        for repository in (self.root, self.source):
            repository.mkdir()
            self.git("init", "-b", "main", root=repository)
            self.identity(repository)
            (repository / "code.c").write_text("initial\n")
            self.git("add", ".", root=repository)
            self.git("commit", "-m", "initial", root=repository)
        self.collaboration = self.base / "collaboration.git"
        self.parent_remote = self.base / "parent.git"
        self.destination = self.base / "vogsphere.git"
        for remote in (self.collaboration, self.parent_remote, self.destination):
            self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(self.collaboration), root=self.source)
        self.git("push", "-u", "origin", "main", root=self.source)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-b", "main",
                 str(self.collaboration), "push_swap")
        self.identity(self.group)
        (self.root / "regular").mkdir()
        (self.root / "regular" / "file").write_text("regular\n")
        self.configure(self.destination)
        self.git("add", ".")
        self.git("commit", "-m", "register group")
        self.git("remote", "add", "origin", str(self.parent_remote))
        self.git("push", "-u", "origin", "main")
        self.initial_parent = self.sha(self.root)
        self.initial_group = self.sha(self.group)

    def git(self, *args, root=None):
        return subprocess.run(["git", *args], cwd=root or self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def identity(self, root):
        self.git("config", "user.name", "Test", root=root)
        self.git("config", "user.email", "test@example.invalid", root=root)

    def sha(self, root):
        return self.git("rev-parse", "HEAD", root=root)

    def configure(self, destination):
        (self.root / ".vogsync.json").write_text(json.dumps({"push_swap": str(destination), "exclude": ["other"]}))

    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(Path(vogsync.__file__).resolve()), *args],
                              cwd=cwd or self.root, capture_output=True, text=True)

    def ok(self, *args, cwd=None):
        result = self.run_cli(*args, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def fails(self, *args, text=None, cwd=None):
        result = self.run_cli(*args, cwd=cwd)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        if text:
            self.assertIn(text, result.stderr)
        return result.stderr

    def change(self, staged=False):
        (self.group / "code.c").write_text("changed\n")
        if staged:
            self.git("add", "code.c", root=self.group)

    def commit(self, *options):
        return self.ok("commit", "push_swap", "-m", "fix: sorting", *options)

    def hook(self, repository, name, contents):
        directory = Path(self.git("rev-parse", "--absolute-git-dir", root=repository)) / "hooks"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_text("#!/bin/sh\n" + contents + "\n")
        path.chmod(0o755)
        return path

    def local_group_commit(self):
        self.change(staged=True)
        self.git("commit", "-m", "direct group commit", root=self.group)

    def test_commit_creates_two_commits_and_warns_without_publishing(self):
        self.change(staged=True)
        output = self.commit()
        self.assertIn("unpublished", output)
        self.assertEqual(self.git("show", "-s", "--format=%s", root=self.group), "fix: sorting")
        self.assertEqual(self.git("show", "-s", "--format=%s"), "chore(push_swap): update group repository revision")
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "push_swap")
        self.assertEqual(vogsync.pointers(self.root, "push_swap"), (self.sha(self.group),) * 2)
        self.assertEqual(self.git("rev-parse", "main", root=self.collaboration), self.initial_group)
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.initial_parent)

    def test_preserves_unrelated_parent_index_and_worktree(self):
        (self.root / "code.c").write_text("staged\n")
        self.git("add", "code.c")
        (self.root / "code.c").write_text("unstaged\n")
        (self.root / "regular" / "file").write_text("also unstaged\n")
        before = self.git("ls-files", "--stage", "--", "code.c", "regular")
        self.change(staged=True)
        self.commit()
        self.assertEqual(self.git("ls-files", "--stage", "--", "code.c", "regular"), before)
        self.assertEqual(self.git("show", ":code.c"), "staged")
        self.assertEqual(self.git("show", "HEAD:code.c"), "initial")
        self.assertEqual((self.root / "code.c").read_text(), "unstaged\n")
        self.assertEqual((self.root / "regular" / "file").read_text(), "also unstaged\n")

    def test_split_index_and_intent_to_add_are_preserved(self):
        (self.root / "intent.c").write_text("not staged\n")
        self.git("add", "-N", "intent.c")
        self.git("update-index", "--assume-unchanged", "code.c")
        self.git("update-index", "--split-index")
        before = self.git("ls-files", "--debug", "--", "code.c", "intent.c")
        self.change(staged=True)
        self.commit()
        self.assertEqual(self.git("ls-files", "--debug", "--", "code.c", "intent.c"), before)
        self.assertNotIn("intent.c", self.git("ls-tree", "--name-only", "HEAD"))

    def test_preserves_another_groups_staged_pointer(self):
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-b", "main", str(self.collaboration), "second")
        second = self.root / "second"
        self.identity(second)
        self.git("commit", "-am", "register second")
        self.git("commit", "--allow-empty", "-m", "second group update", root=second)
        self.git("add", "second")
        staged = self.sha(second)
        self.change(staged=True)
        self.commit()
        self.assertEqual(vogsync.pointers(self.root, "second"), (self.initial_group, staged))
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "push_swap")

    def test_all_excludes_untracked_group_files(self):
        self.change()
        (self.group / "new.c").write_text("new\n")
        self.commit("-a")
        self.assertEqual(self.git("show", "HEAD:code.c", root=self.group), "changed")
        self.assertNotIn("new.c", self.git("ls-tree", "--name-only", "HEAD", root=self.group))
        self.assertIn("?? new.c", self.git("status", "--short", root=self.group))

    def test_without_all_only_group_staged_changes_are_committed(self):
        self.change(staged=True)
        (self.group / "code.c").write_text("not staged\n")
        self.commit()
        self.assertEqual(self.git("show", "HEAD:code.c", root=self.group), "changed")
        self.assertEqual((self.group / "code.c").read_text(), "not staged\n")

    def test_routing_from_inside_group_and_subdirectory(self):
        sub = self.group / "src"
        sub.mkdir()
        (sub / "new.c").write_text("source\n")
        self.ok("add", cwd=sub)
        self.assertIn("src/new.c", self.ok("diff", "--cached", cwd=sub))
        self.ok("commit", "-m", "feat: source", cwd=sub)
        self.assertEqual(vogsync.pointers(self.root, "push_swap")[0], self.sha(self.group))
        self.assertIn("feat: source", self.ok("log", cwd=sub))
        output = self.ok("status", cwd=sub)
        self.assertIn(str(self.root), output)
        self.assertIn("Inside group", output)
        self.assertIn(str(self.destination), self.ok("remote", "get", "push_swap", cwd=sub))

    def test_add_diff_log_restore_routing(self):
        self.change()
        self.assertIn("+changed", self.ok("diff", "push_swap"))
        self.ok("add", "push_swap")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertIn("+changed", self.ok("diff", "push_swap", "--cached"))
        self.ok("restore", "push_swap", "--staged")
        self.assertEqual(self.git("diff", "--cached", "--name-only", root=self.group), "")
        self.assertEqual((self.group / "code.c").read_text(), "changed\n")
        self.ok("restore", "push_swap")
        self.assertEqual((self.group / "code.c").read_text(), "initial\n")
        self.assertIn("initial", self.ok("log", "push_swap"))

    def test_no_pin_and_recovery_no_op(self):
        self.change(staged=True)
        self.commit("--no-pin")
        self.assertEqual(self.sha(self.root), self.initial_parent)
        self.assertEqual(vogsync.pointers(self.root, "push_swap"), (self.initial_group,) * 2)
        self.assertIn("unpublished", self.ok("pin", "push_swap"))
        recorded = self.sha(self.root)
        self.assertIn("nothing to pin", self.ok("pin", "push_swap"))
        self.assertEqual(self.sha(self.root), recorded)

    def test_pin_allows_already_staged_target_but_commit_does_not_overwrite_it(self):
        self.local_group_commit()
        self.git("add", "push_swap")
        old = self.sha(self.group)
        self.change(staged=True)
        (self.group / "code.c").write_text("second\n")
        self.git("add", "code.c", root=self.group)
        self.fails("commit", "push_swap", "-m", "fix", text="already-staged")
        self.assertEqual(self.sha(self.group), old)
        self.ok("pin", "push_swap")
        self.assertEqual(vogsync.pointers(self.root, "push_swap"), (old,) * 2)

    def test_pin_rejects_other_staged_pointer(self):
        self.local_group_commit()
        self.git("add", "push_swap")
        (self.group / "code.c").write_text("second\n")
        self.git("commit", "-am", "second", root=self.group)
        self.fails("pin", "push_swap", text="already-staged")
        self.assertEqual(self.sha(self.root), self.initial_parent)

    def test_group_commit_hook_failure_creates_neither_commit(self):
        self.change(staged=True)
        self.hook(self.group, "pre-commit", "exit 1")
        self.fails("commit", "push_swap", "-m", "fix", text="failed")
        self.assertEqual(self.sha(self.group), self.initial_group)
        self.assertEqual(self.sha(self.root), self.initial_parent)

    def test_parent_hook_failure_retains_group_commit_and_recovers(self):
        self.change(staged=True)
        hook = self.hook(self.root, "pre-commit", "exit 1")
        self.fails("commit", "push_swap", "-m", "fix", text="vogsync pin push_swap")
        self.assertNotEqual(self.sha(self.group), self.initial_group)
        self.assertEqual(self.sha(self.root), self.initial_parent)
        self.assertEqual(vogsync.pointers(self.root, "push_swap"), (self.initial_group,) * 2)
        hook.unlink()
        self.ok("pin", "push_swap")

    def test_parent_hook_cannot_include_unrelated_files(self):
        self.change(staged=True)
        (self.root / "code.c").write_text("unrelated\n")
        self.git("add", "code.c")
        before = self.git("ls-files", "--stage")
        self.hook(self.root, "pre-commit", "git add code.c")
        self.fails("commit", "push_swap", "-m", "fix", text="pointer-only index")
        self.assertEqual(self.sha(self.root), self.initial_parent)
        self.assertEqual(self.git("ls-files", "--stage"), before)

    def test_parent_hook_changing_group_head_stops_pin(self):
        self.change(staged=True)
        self.hook(self.root, "pre-commit", 'unset GIT_INDEX_FILE; git -C push_swap -c core.hooksPath=/dev/null commit --allow-empty -m "hook commit"')
        self.fails("commit", "push_swap", "-m", "fix", text="state changed")
        self.assertEqual(self.sha(self.root), self.initial_parent)

    def test_parent_commit_message_hooks_run(self):
        self.change(staged=True)
        self.hook(self.root, "commit-msg", 'echo "Reviewed-by: Test" >> "$1"')
        self.commit()
        self.assertIn("Reviewed-by: Test", self.git("show", "-s", "--format=%B"))

    def test_parent_hook_empty_message_is_rejected_without_losing_group_commit(self):
        self.change(staged=True)
        self.hook(self.root, "commit-msg", ': > "$1"')
        self.fails("commit", "push_swap", "-m", "fix", text="emptied the commit message")
        self.assertNotEqual(self.sha(self.group), self.initial_group)
        self.assertEqual(self.sha(self.root), self.initial_parent)

    def test_post_commit_hook_failure_reports_retained_parent(self):
        self.change(staged=True)
        self.hook(self.root, "post-commit", "exit 1")
        self.fails("commit", "push_swap", "-m", "fix", text="post-commit hook failed")
        self.assertEqual(vogsync.pointers(self.root, "push_swap"), (self.sha(self.group),) * 2)

    def test_preflight_missing_identity_in_either_repository(self):
        self.change(staged=True)
        for repository in (self.root, self.group):
            self.git("config", "user.useConfigOnly", "true", root=repository)
            self.git("config", "--unset", "user.email", root=repository)
            self.fails("commit", "push_swap", "-m", "fix")
            self.assertEqual(self.sha(self.group), self.initial_group)
            self.assertEqual(self.sha(self.root), self.initial_parent)
            self.identity(repository)

    def test_preflight_operations_and_conflicts(self):
        self.change(staged=True)
        for repository in (self.root, self.group):
            gitdir = Path(self.git("rev-parse", "--absolute-git-dir", root=repository))
            marker = gitdir / "CHERRY_PICK_HEAD"
            marker.write_text(self.sha(repository))
            self.fails("commit", "push_swap", "-m", "fix", text="operation in progress")
            marker.unlink()
        self.assertEqual(self.sha(self.group), self.initial_group)

    def test_preflight_index_and_branch_locks(self):
        self.change(staged=True)
        for name in ("index.lock", "refs/heads/main.lock"):
            lock = self.root / ".git" / name
            lock.write_text("owned by another process")
            self.fails("commit", "push_swap", "-m", "fix", text="operation in progress")
            self.assertEqual(lock.read_text(), "owned by another process")
            self.assertEqual(self.sha(self.group), self.initial_group)
            lock.unlink()

    def test_conflicted_parent_index_stops_before_group_commit(self):
        self.change(staged=True)
        blob = self.git("rev-parse", "HEAD:code.c")
        subprocess.run(["git", "update-index", "--index-info"], cwd=self.root, check=True,
                       input="0 {}\tcode.c\n100644 {} 1\tcode.c\n100644 {} 2\tcode.c\n".format("0" * 40, blob, blob),
                       capture_output=True, text=True)
        before = self.git("ls-files", "--stage")
        self.fails("commit", "push_swap", "-m", "fix", text="Unresolved merge conflict")
        self.assertEqual(self.sha(self.group), self.initial_group)
        self.assertEqual(self.git("ls-files", "--stage"), before)

    def test_uncommitted_registration_changes_block_automatic_pin_preflight(self):
        self.change(staged=True)
        with (self.root / ".gitmodules").open("a") as handle:
            handle.write("\n# uncommitted registration edit\n")
        self.fails("commit", "push_swap", "-m", "fix", text=".gitmodules registration changes")
        self.assertEqual(self.sha(self.group), self.initial_group)

    def test_parent_hooks_starting_git_operation_block_pin(self):
        self.change(staged=True)
        self.hook(self.root, "pre-commit", 'git rev-parse HEAD > "$(git rev-parse --git-path CHERRY_PICK_HEAD)"')
        self.fails("commit", "push_swap", "-m", "fix", text="operation in progress")
        self.assertEqual(self.sha(self.root), self.initial_parent)
        self.assertNotEqual(self.sha(self.group), self.initial_group)

    def test_parent_hook_concurrent_branch_update_is_not_overwritten(self):
        self.change(staged=True)
        self.hook(self.root, "pre-commit", 'tree=$(git rev-parse HEAD^{tree}); other=$(echo concurrent | git commit-tree "$tree" -p HEAD); git update-ref refs/heads/main "$other"')
        self.fails("commit", "push_swap", "-m", "fix", text="cannot lock ref")
        self.assertEqual(self.git("show", "-s", "--format=%s"), "concurrent")
        self.assertEqual(vogsync.pointers(self.root, "push_swap"), (self.initial_group,) * 2)

    def test_parent_hook_replacing_index_lock_is_not_overwritten_or_deleted(self):
        self.change(staged=True)
        self.hook(self.root, "pre-commit", 'lock=$(git rev-parse --git-path index.lock); mv "$lock" "$lock.saved"; echo "other lock" > "$lock"')
        self.fails("commit", "push_swap", "-m", "fix", text="index/lock changed")
        self.assertEqual((self.root / ".git" / "index.lock").read_text(), "other lock\n")
        self.assertEqual(self.sha(self.root), self.initial_parent)

    def test_group_push_hook_state_changes_prevent_parent_push(self):
        self.change(staged=True)
        self.hook(self.group, "pre-push", 'git -c core.hooksPath=/dev/null commit --allow-empty -m "push hook commit"')
        self.fails("commit", "push_swap", "-m", "fix", "--push", text="repository state changed")
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.initial_parent)

    def test_group_commit_hook_state_change_keeps_commits_and_stops_pin(self):
        self.change(staged=True)
        self.hook(self.group, "post-commit", 'git -c core.hooksPath=/dev/null commit --allow-empty -m "hook commit"')
        self.fails("commit", "push_swap", "-m", "fix", text="hooks changed branch/history")
        self.assertEqual(self.git("rev-list", "--count", "HEAD", root=self.group), "3")
        self.assertEqual(self.sha(self.root), self.initial_parent)

    def test_deleted_group_checkout_reports_initialization_guidance(self):
        self.git("submodule", "deinit", "-f", "--", "push_swap")
        self.group.rmdir()
        self.fails("add", "push_swap", text="submodule update --init")

    def test_relative_clone_url_and_named_submodule(self):
        # Registration names need not equal their path. Relative clone URLs
        # stay in .gitmodules; actual upstreams come from the group config.
        self.git("config", "--file", ".gitmodules", "--rename-section", "submodule.push_swap", "submodule.collaborators")
        self.git("config", "--file", ".gitmodules", "submodule.collaborators.url", "../collaboration.git")
        self.git("add", ".gitmodules")
        self.git("commit", "-m", "relative registration")
        self.change(staged=True)
        self.commit()
        self.ok("deploy", "push_swap", "--dry-run")

    def test_recurse_submodules_clone_and_context_discovery(self):
        clone = self.base / "recursive clone"
        self.git("-c", "protocol.file.allow=always", "clone", "--recurse-submodules", "--branch", "main", str(self.parent_remote), str(clone))
        group = clone / "push_swap"
        self.assertTrue((group / ".git").is_file())
        self.git("switch", "-c", "main", "--track", "origin/main", root=group)
        for repository in (clone, group):
            self.identity(repository)
        (group / "code.c").write_text("cloned change\n")
        self.ok("commit", "-a", "-m", "fix: cloned", cwd=group)
        self.assertEqual(vogsync.pointers(clone, "push_swap"), (self.sha(group),) * 2)

    def test_group_dry_run_does_not_require_commit_identity(self):
        for repository in (self.root, self.group):
            self.git("config", "user.useConfigOnly", "true", root=repository)
            self.git("config", "--unset", "user.email", root=repository)
        self.ok("deploy", "push_swap", "--dry-run")

    def test_relative_clone_destination_protected_even_after_changing_group_origin(self):
        self.git("config", "--file", ".gitmodules", "submodule.push_swap.url", "../collaboration.git")
        self.configure(self.collaboration)
        self.git("add", ".gitmodules", ".vogsync.json")
        self.git("commit", "-m", "relative registration and unsafe destination")
        self.git("remote", "set-url", "origin", str(self.destination), root=self.group)
        self.fails("deploy", "push_swap", "--dry-run", text="separate Vogsphere")

    def test_remote_status_compares_direct_group_head(self):
        self.git("push", str(self.destination), "HEAD:refs/heads/master", root=self.group)
        self.assertIn("push_swap: in sync", self.ok("status", "push_swap", "--remote"))

    def test_doctor_rejects_collaboration_destination(self):
        self.configure(self.collaboration)
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("separate Vogsphere", result.stdout)

    def test_missing_push_upstream_preflight_creates_neither_commit(self):
        self.change(staged=True)
        for repository in (self.root, self.group):
            self.git("branch", "--unset-upstream", root=repository)
            self.fails("commit", "push_swap", "-m", "fix", "--push", text="no upstream")
            self.assertEqual(self.sha(self.group), self.initial_group)
            self.assertEqual(self.sha(self.root), self.initial_parent)
            self.git("branch", "--set-upstream-to=origin/main", root=repository)

    def test_detached_group_blocks_commit_pin_deploy_but_not_diagnostics(self):
        self.git("checkout", "--detach", root=self.group)
        self.change(staged=True)
        for command in (("commit", "push_swap", "-m", "fix"), ("pin", "push_swap"), ("deploy", "push_swap", "--dry-run")):
            self.fails(*command, text="detached")
        self.assertIn("detached", self.ok("status", "push_swap"))
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("detached", result.stdout)

    def test_uninitialized_group_is_diagnosed_without_initializing(self):
        self.git("submodule", "deinit", "-f", "--", "push_swap")
        self.assertIn("uninitialized", self.ok("status", "push_swap"))
        self.fails("add", "push_swap", text="uninitialized")
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("submodule update --init", result.stdout)
        self.assertFalse((self.group / ".git").exists())

    def test_malformed_registrations_and_unregistered_gitlink_rejected(self):
        modules = self.root / ".gitmodules"
        original = modules.read_text()
        for contents in ('[submodule "bad"]\npath = ../bad\nurl = nowhere\n',
                         '[submodule "bad"]\npath = push_swap\n',
                         original + original, "", "invalid syntax"):
            modules.write_text(contents)
            self.fails("add", "push_swap")
            self.assertEqual(self.sha(self.group), self.initial_group)
        modules.write_text(original)

    def test_accidental_nested_repository_cannot_hide_parent(self):
        nested = self.root / "regular" / "nested"
        nested.mkdir()
        self.git("init", root=nested)
        self.fails("add", "regular", text="Nested Git repository")
        self.fails("commit", "-m", "bad", cwd=nested, text="Nested Git repository")

    def test_nested_submodule_layout_not_supported(self):
        self.git("update-index", "--add", "--cacheinfo", "160000,{},nested".format(self.initial_group), root=self.group)
        self.fails("add", "push_swap", text="Nested submodules")

    def test_local_status_and_doctor_no_network_or_writes(self):
        def snapshot():
            return {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        before = snapshot()
        output = io.StringIO()
        with patch.object(vogsync, "remote_head", side_effect=AssertionError("network")), contextlib.redirect_stdout(output):
            self.assertTrue(vogsync.command_doctor(self.root, False, "master"))
            vogsync.command_status(self.root, False, "push_swap")
        self.assertEqual(before, snapshot())
        self.assertIn("Parent index pointer", output.getvalue())
        self.assertIn("Upstream", output.getvalue())

    def test_missing_deployment_configuration_not_needed_for_commits(self):
        self.configure_removed()
        self.change(staged=True)
        self.commit()
        self.assertIn("No project remote configured", self.ok("status", "push_swap"))
        self.fails("deploy", "push_swap", "--dry-run", text="No remote configured")

    def configure_removed(self):
        (self.root / ".vogsync.json").write_text('{"exclude": ["other"]}')
        self.git("add", ".vogsync.json")
        self.git("commit", "-m", "remove destination")

    def test_regular_project_staging_semantics_and_config_compatibility(self):
        (self.root / "regular" / "file").write_text("changed\n")
        (self.root / "code.c").write_text("unrelated\n")
        self.git("add", "code.c")
        self.ok("commit", "regular", "-a", "-m", "fix")
        self.assertEqual(self.git("show", "HEAD:regular/file"), "changed")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "code.c")
        self.ok("commit", "regular", "-m", "index")
        self.assertEqual(self.git("show", "HEAD:code.c"), "unrelated")
        self.ok("remote", "set", "push_swap", str(self.destination))
        self.assertEqual(vogsync.load_settings(self.root)[1], ["other"])

    def test_push_order_and_no_force_to_collaboration(self):
        self.change(staged=True)
        args = vogsync.build_parser().parse_args(["commit", "push_swap", "-m", "fix", "--push"])
        original = vogsync.git
        pushes = []

        def record(arguments, root=None, **kwargs):
            if arguments[0] == "push":
                pushes.append((root, arguments))
            return original(arguments, root, **kwargs)

        with patch.object(vogsync, "git", side_effect=record), contextlib.redirect_stdout(io.StringIO()):
            vogsync.command_commit(args, self.root)
        self.assertEqual([root for root, _ in pushes], [self.group, self.root])
        self.assertTrue(all(arguments == ["push", "--", "origin", "HEAD:refs/heads/main"] for _, arguments in pushes))
        self.assertEqual(self.git("rev-parse", "main", root=self.collaboration), self.sha(self.group))
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.sha(self.root))

    def test_no_pin_push_only_publishes_group(self):
        self.change(staged=True)
        self.assertIn("only the group was pushed", self.commit("--no-pin", "--push"))
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.initial_parent)
        self.assertEqual(self.git("rev-parse", "main", root=self.collaboration), self.sha(self.group))

    def test_rejected_group_push_stops_parent_and_retains_commits(self):
        self.hook(self.collaboration, "pre-receive", "exit 1")
        self.change(staged=True)
        self.fails("commit", "push_swap", "-m", "fix", "--push", text="Group push failed")
        self.assertNotEqual(self.sha(self.group), self.initial_group)
        self.assertNotEqual(self.sha(self.root), self.initial_parent)
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.initial_parent)

    def test_parent_push_failure_reports_published_group(self):
        self.hook(self.parent_remote, "pre-receive", "exit 1")
        self.change(staged=True)
        self.fails("commit", "push_swap", "-m", "fix", "--push", text="Group push succeeded, but parent push failed")
        self.assertEqual(self.git("rev-parse", "main", root=self.collaboration), self.sha(self.group))
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.initial_parent)

    def test_deploy_direct_history_files_and_push_order(self):
        self.change(staged=True)
        self.commit()
        args = vogsync.build_parser().parse_args(["deploy", "push_swap", "--branch", "submission"])
        original = vogsync.git
        pushes = []

        def record(arguments, root=None, **kwargs):
            if arguments[0] == "push":
                pushes.append((root, arguments))
            if "subtree" in arguments:
                self.fail("group history must not be split")
            return original(arguments, root, **kwargs)

        with patch.object(vogsync, "git", side_effect=record), contextlib.redirect_stdout(io.StringIO()):
            vogsync.command_deploy(args, self.root)
        self.assertEqual([root for root, _ in pushes], [self.group, self.root, self.group])
        self.assertNotIn("--force", pushes[0][1])
        self.assertNotIn("--force", pushes[1][1])
        self.assertIn("--force", pushes[2][1])
        self.assertEqual(self.git("rev-parse", "submission", root=self.destination), self.sha(self.group))
        self.assertEqual(self.git("ls-tree", "--name-only", "submission", root=self.destination), "code.c")
        self.assertEqual(self.git("show", "submission:code.c", root=self.destination), "changed")
        self.assertEqual(self.git("rev-list", "--count", "submission", root=self.destination), "2")

    def test_deploy_dry_run_no_push_pointer_or_checkout_changes(self):
        before = (self.sha(self.root), self.sha(self.group), self.git("ls-files", "--stage"))
        self.ok("deploy", "push_swap", "--dry-run")
        self.assertEqual(before, (self.sha(self.root), self.sha(self.group), self.git("ls-files", "--stage")))
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.initial_parent)
        self.assertEqual(self.git("ls-remote", str(self.destination)), "")
        self.fails("deploy", "push_swap", "--pull", "--dry-run", text="--pull is unsupported")

    def test_deploy_blocks_unrecorded_or_staged_pointer_and_dirty_group(self):
        self.local_group_commit()
        self.fails("deploy", "push_swap", "--dry-run", text="pointers must match")
        self.git("add", "push_swap")
        self.fails("deploy", "push_swap", "--dry-run", text="pointers must match")
        self.ok("pin", "push_swap")
        self.change()
        (self.group / "code.c").write_text("dirty\n")
        self.fails("deploy", "push_swap", "--dry-run", text="dirty")

    def test_deploy_rejects_main_group_and_rewritten_destinations(self):
        for destination in (self.parent_remote, self.collaboration, self.root, self.group):
            self.configure(destination)
            self.git("add", ".vogsync.json")
            self.git("commit", "-m", "destination")
            self.fails("deploy", "push_swap", "--dry-run", text="separate Vogsphere")
        self.configure("unsafe:group")
        self.git("add", ".vogsync.json")
        self.git("commit", "-m", "rewritten destination")
        self.git("config", "url.{}.insteadOf".format(self.collaboration), "unsafe:group")
        self.fails("deploy", "push_swap", "--dry-run", text="separate Vogsphere")

    def test_push_url_rewrite_destination_rejected(self):
        self.configure(self.destination)
        self.git("config", "url.{}.pushInsteadOf".format(self.collaboration), str(self.destination), root=self.group)
        self.fails("deploy", "push_swap", "--dry-run", text="separate Vogsphere")

    def test_deploy_group_push_failure_leaves_other_destinations_untouched(self):
        self.change(staged=True)
        self.commit()
        self.hook(self.collaboration, "pre-receive", "exit 1")
        self.fails("deploy", "push_swap", text="Group push failed")
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.initial_parent)
        self.assertEqual(self.git("ls-remote", str(self.destination)), "")

    def test_deploy_parent_push_failure_leaves_vogsphere_untouched(self):
        self.change(staged=True)
        self.commit()
        self.hook(self.parent_remote, "pre-receive", "exit 1")
        self.fails("deploy", "push_swap", text="Group push succeeded, but parent push failed")
        self.assertEqual(self.git("rev-parse", "main", root=self.collaboration), self.sha(self.group))
        self.assertEqual(self.git("ls-remote", str(self.destination)), "")

    def test_deploy_final_failure_reports_both_published_collaboration_repos(self):
        self.change(staged=True)
        self.commit()
        self.hook(self.destination, "pre-receive", "exit 1")
        self.fails("deploy", "push_swap", text="Group and main repo pushes succeeded, but Vogsphere deployment failed")
        self.assertEqual(self.git("rev-parse", "main", root=self.collaboration), self.sha(self.group))
        self.assertEqual(self.git("rev-parse", "main", root=self.parent_remote), self.sha(self.root))


if __name__ == "__main__":
    unittest.main()
