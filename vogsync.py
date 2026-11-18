#!/usr/bin/env python3
"""Manage project-specific Vogsphere deployments from a main repo."""

CLI_NAME = "vogsync"
CONFIG_FILE_NAME = ".vogsync.json"

import argparse
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

VERSION = "1.0.0"
PROG = CLI_NAME
COLORS = {"success": "\033[32m", "warning": "\033[33m", "error": "\033[31m", "info": "\033[36m"}


class ToolError(Exception):
    """An operational error suitable for display without a traceback."""


def say(message: str, level: str = "info", error: bool = False) -> None:
    """Print a message with color when standard output is a terminal."""
    if os.name != "nt" and sys.stdout.isatty() and level in COLORS:
        message = COLORS[level] + message + "\033[0m"
    print(message, file=sys.stderr if error else sys.stdout)


def git(args: Sequence[str], root: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """Run Git without a shell and provide a useful error on failure."""
    try:
        return subprocess.run(["git", *args], cwd=str(root) if root else None,
                              check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    except FileNotFoundError:
        raise ToolError("Git was not found. Install Git and make sure it is in PATH.")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or "Git exited with status {}.".format(exc.returncode)
        raise ToolError("git {} failed:\n{}".format(" ".join(args), detail))
    except OSError as exc:
        raise ToolError("Could not run Git: {}".format(exc))


def repository_root() -> Path:
    """Resolve the repository root, including worktrees and submodules."""
    root = Path(git(["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    # Also inspect ancestors: accidental nested repositories must not hide the
    # monorepo, and an uninitialized checkout has no superproject Git metadata.
    for parent in root.parents:
        if (parent / ".git").exists():
            groups = registered_groups(parent)
            relative = root.relative_to(parent)
            if len(relative.parts) != 1 or relative.name not in groups:
                raise ToolError("Nested Git repository at {}; only registered top-level submodules are supported.".format(relative))
            return parent
    return root


def index_entries(root: Path) -> Dict[str, Tuple[str, str, str]]:
    """Read stage-zero entries without refreshing or writing the index."""
    entries = {}
    for entry in git(["ls-files", "--stage", "-z"], root).stdout.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, sha, stage = metadata.split()
            if stage != "0":
                raise ToolError("Unresolved merge conflict at {}.".format(path))
            entries[path] = (mode, sha, stage)
    return entries


def registered_groups(root: Path) -> Dict[str, str]:
    """Accept only .gitmodules registrations backed by committed/index gitlinks."""
    modules = root / ".gitmodules"
    entries = index_entries(root)
    links = {path for path, value in entries.items() if value[0] == "160000"}
    groups = {}
    if modules.is_symlink():
        raise ToolError(".gitmodules must not be a symlink.")
    if modules.exists():
        data = git(["config", "--null", "--file", str(modules), "--list"], root).stdout
        sections = {}
        for item in data.split("\0"):
            if not item:
                continue
            key, _, value = item.partition("\n")
            match = re.fullmatch(r"submodule\.(.+)\.([^.]+)", key)
            if match:
                section = sections.setdefault(match[1], {})
                if match[2] not in ("path", "url"):
                    continue
                if match[2] in section:
                    raise ToolError("Duplicate .gitmodules registration: {}.".format(key))
                section[match[2]] = value
        for section in sections.values():
            if set(section) != {"path", "url"}:
                raise ToolError("Malformed .gitmodules registration: each group needs path and url.")
            path, url = section["path"], section["url"]
            validate_project(path)
            check_url(url)
            if path in groups or path not in links:
                raise ToolError("Malformed submodule registration at {}: expected one index gitlink.".format(path))
            committed = git(["ls-tree", "HEAD", "--", ":(literal)" + path], root).stdout
            if not committed.startswith("160000 commit "):
                raise ToolError("Commit the submodule registration for {} first.".format(path))
            if (root / path).is_symlink():
                raise ToolError("Group path must not be a symlink: {}.".format(path))
            groups[path] = url
    if groups:
        registration = entries.get(".gitmodules")
        committed_modules = git(["ls-tree", "HEAD", "--", ".gitmodules"], root).stdout
        if (not registration or registration[0] not in ("100644", "100755") or
                not committed_modules.startswith(("100644 blob ", "100755 blob "))):
            raise ToolError("Commit .gitmodules as a regular file before using group repositories.")
    if links != set(groups):
        raise ToolError("Submodule/gitlink at {} has no valid .gitmodules registration.".format(", ".join(sorted(links - set(groups)))))
    return groups


def group_repository(root: Path, project: str) -> Optional[Path]:
    groups = registered_groups(root)
    if project not in groups:
        return None
    path = root / project
    if not (path / ".git").exists():
        raise ToolError("Group {} is uninitialized. Run 'git submodule update --init -- {}' explicitly, then select a branch with 'git -C {} switch <branch>'.".format(project, project, project))
    if Path(git(["rev-parse", "--show-toplevel"], path).stdout.strip()).resolve() != path.resolve():
        raise ToolError("Invalid group checkout: {}.".format(path))
    check_layout(path, {})
    return path


def check_layout(root: Path, groups: Dict[str, str]) -> None:
    """Never relax nested repository checks outside registered group roots."""
    def scan_error(exc: OSError) -> None:
        raise ToolError("Could not inspect project layout: {}".format(exc))

    for directory, directories, files in os.walk(root, followlinks=False, onerror=scan_error):
        path = Path(directory)
        if path != root and (".git" in directories or ".git" in files):
            relative = path.relative_to(root).as_posix()
            if relative not in groups:
                raise ToolError("Nested Git repository at {}; only registered top-level groups are supported.".format(relative))
        if ".git" in directories:
            directories.remove(".git")
    if not groups and any(value[0] == "160000" for value in index_entries(root).values()):
        raise ToolError("Nested submodules inside group repositories are unsupported.")


def active_project(root: Path) -> Optional[str]:
    """Return the top-level directory containing the working directory."""
    try:
        parts = Path.cwd().resolve().relative_to(root).parts
    except ValueError:
        raise ToolError("The current directory is outside the repository root.")
    return parts[0] if parts else None


def validate_project(project: str) -> None:
    """Accept only a single, non-special top-level directory name."""
    if not project or project in (".", "..", ".git") or ntpath.splitdrive(project)[0] or any(c in project for c in ("/", "\\", "\x00")):
        raise ToolError("Project must be a single top-level directory name.")


def project_directory(root: Path, project: str) -> Path:
    """Ensure the named project exists as a real directory."""
    validate_project(project)
    path = root / project
    groups = registered_groups(root)
    if project in groups and not path.exists():
        raise ToolError("Group {} is uninitialized. Run 'git submodule update --init -- {}' explicitly, then select its branch.".format(project, project))
    if path.is_symlink() or not path.is_dir():
        raise ToolError("Project must be an existing, non-symlink directory: {}".format(path))
    check_layout(root, groups)
    return path


def check_url(url: str) -> None:
    """Reject empty values and values that Git could interpret as options."""
    if not url or url.startswith("-") or "\x00" in url:
        raise ToolError("Remote URL must be nonempty and must not begin with '-'.")


def load_settings(root: Path) -> Tuple[Dict[str, str], List[str]]:
    """Read project URLs and optional top-level directory exclusions."""
    path = root / CONFIG_FILE_NAME
    if not path.exists():
        return {}, []
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolError("Could not read {}: {}".format(path, exc))
    if not isinstance(data, dict):
        raise ToolError("{} must contain a JSON object.".format(path))
    excluded = data.pop("exclude", [])
    if not isinstance(excluded, list) or any(not isinstance(name, str) for name in excluded):
        raise ToolError("{}: exclude must be a list of top-level directory names.".format(path))
    for name in excluded:
        validate_project(name)
    result: Dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise ToolError("{} must map project names to nonempty URLs.".format(path))
        validate_project(key)
        check_url(value)
        result[key] = value
    if set(excluded) & result.keys():
        raise ToolError("{}: excluded directories cannot also have project Vogsphere URLs.".format(path))
    return result, excluded


def load_config(root: Path) -> Dict[str, str]:
    """Read the project-to-URL mapping."""
    return load_settings(root)[0]


def save_config(root: Path, config: Dict[str, str]) -> None:
    """Save the configuration with restrictive permissions on creation."""
    _, excluded = load_settings(root)
    if set(excluded) & config.keys():
        raise ToolError("Remove the project from exclude before configuring its Vogsphere URL.")
    settings: Dict[str, object] = dict(config)
    if excluded:
        settings["exclude"] = excluded
    path = root / CONFIG_FILE_NAME
    if path.is_symlink():
        raise ToolError("Refusing to overwrite symlink: {}".format(path))
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise ToolError("Could not write {}: {}".format(path, exc))


def configured_url(config: Dict[str, str], project: str) -> str:
    """Get a remote URL or explain how to configure it."""
    if project not in config:
        raise ToolError("No remote configured for {!r}. Run '{} remote set {} <url>'.".format(project, PROG, project))
    return config[project]


def current_branch(root: Path) -> Optional[str]:
    """Return the current branch, or None for a detached HEAD."""
    try:
        result = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                                cwd=str(root), check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return None
        raise ToolError((exc.stderr or "Could not determine current branch.").strip())
    except OSError as exc:
        raise ToolError("Could not determine current branch: {}".format(exc))


def upstream(root: Path) -> str:
    """Ensure the current branch has an upstream and return its name."""
    if current_branch(root) is None:
        raise ToolError("HEAD is detached. Check out a branch with an upstream first.")
    try:
        return git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], root).stdout.strip()
    except ToolError:
        raise ToolError("The current branch has no upstream. Configure one with 'git push -u <remote> <branch>' first.")


def push_upstream(root: Path) -> None:
    """Push HEAD explicitly to the configured upstream, regardless of push.default."""
    branch = current_branch(root)
    upstream(root)
    assert branch is not None
    remote = git(["config", "--get", "branch.{}.remote".format(branch)], root).stdout.strip()
    merge = git(["config", "--get", "branch.{}.merge".format(branch)], root).stdout.strip()
    if not remote or remote.startswith("-") or not merge.startswith("refs/heads/"):
        raise ToolError("Invalid upstream configuration for branch {!r}.".format(branch))
    git(["push", "--", remote, "HEAD:" + merge], root)


def require_head(root: Path) -> None:
    """Ensure the repository has at least one commit."""
    try:
        git(["rev-parse", "--verify", "HEAD"], root)
    except ToolError:
        raise ToolError("The repository has no commit yet. Create an initial commit first.")


def split_project(root: Path, project: str) -> str:
    """Compute a subtree split and return its commit object ID."""
    output = git(["subtree", "split", "--prefix=" + project], root).stdout.strip().splitlines()
    if not output:
        raise ToolError("No committed history found for {!r}; commit its files first.".format(project))
    sha = output[-1].strip()
    # Git repositories can use either SHA-1 or SHA-256 object IDs.
    if len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
        raise ToolError("Failed to parse split commit ID: {}".format(sha))
    return sha


def remote_head(root: Path, url: str, branch: str) -> Optional[str]:
    """Query a remote branch, returning None if it does not exist."""
    check_url(url)
    env = dict(os.environ, GIT_SSH_COMMAND="ssh -o ConnectTimeout=2 -o BatchMode=yes")
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "--", url, "refs/heads/" + branch],
            cwd=str(root), check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise ToolError("Remote check timed out (is the Vogsphere network reachable?).")
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = (exc.stderr or "").strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise ToolError("Remote check failed (is the Vogsphere network reachable?): {}".format(detail))
    lines = result.stdout.strip().splitlines()
    return lines[0].split()[0] if lines else None


def command_remote(args: argparse.Namespace, root: Path) -> None:
    """Manage project remote mappings."""
    config = load_config(root)
    if args.remote_command == "set":
        if args.project == "exclude":
            raise ToolError("'exclude' is reserved for directory exclusions in {}.".format(CONFIG_FILE_NAME))
        project_directory(root, args.project)
        check_url(args.url)
        config[args.project] = args.url
        save_config(root, config)
        say("Remote configured for {}.".format(args.project), "success")
    elif args.remote_command == "get":
        validate_project(args.project)
        print(configured_url(config, args.project))
    elif not config:
        say("No project remotes configured.")
    else:
        width = max(len("PROJECT"), *(len(name) for name in config))
        print("{:<{}}  {}".format("PROJECT", width, "URL"))
        for project, url in sorted(config.items()):
            print("{:<{}}  {}".format(project, width, url))


def command_commit(args: argparse.Namespace, root: Path) -> None:
    """Commit the index, optionally including tracked changes and pushing."""
    check_layout(root, registered_groups(root))
    project = args.project or active_project(root)
    if project and group_repository(root, project):
        command_group_commit(args, root, project)
        return
    if args.no_pin:
        raise ToolError("--no-pin is only supported for group repositories.")
    if project:
        project_directory(root, project)
        message = "[{}] {}".format(project, args.message)
    else:
        say("At main repo root: creating an unprefixed commit.", "warning")
        message = args.message
    if not args.message.strip():
        raise ToolError("Commit message must not be empty.")
    if args.push:
        upstream(root)
    command = ["commit"]
    if args.all:
        command.append("--only" if project else "--all")
    command.extend(["-m", message])
    if args.all and project:
        command.extend(["--", ":(literal)" + project])
    result = git(command, root)
    if result.stdout.strip():
        print(result.stdout.strip())
    say("Commit created.", "success")
    if args.push:
        push_upstream(root)
        say("Pushed to the configured upstream.", "success")


def command_deploy(args: argparse.Namespace, root: Path) -> None:
    """Back up the main repo to its upstream, then deploy a project split."""
    project_directory(root, args.project)
    if group_repository(root, args.project):
        command_group_deploy(args, root)
        return
    url = configured_url(load_config(root), args.project)
    check_url(url)
    safe_destination(root, url)
    if not args.branch or args.branch.startswith("-"):
        raise ToolError("Invalid target branch: {!r}".format(args.branch))
    git(["check-ref-format", "refs/heads/" + args.branch], root)
    require_head(root)
    if git(["status", "--porcelain"], root).stdout.strip():
        raise ToolError("Working tree is dirty. Commit or stash changes before deploying.")
    tracking = upstream(root)
    if args.pull:
        git(["pull", "--ff-only"], root)
        say("Updated from the configured upstream.", "success")
        project_directory(root, args.project)
    say("Splitting committed history for {}...".format(args.project))
    commit = split_project(root, args.project)
    if args.dry_run:
        remote_head(root, url, args.branch)
        say("Dry run successful: split {} and reached the remote; nothing pushed.".format(commit), "success")
    else:
        say("Pushing main repo to {}...".format(tracking))
        try:
            push_upstream(root)
        except ToolError as exc:
            raise ToolError("Main repo push failed; Vogsphere was not updated.\n{}".format(exc))
        say("Main repo pushed to {}.".format(tracking), "success")
        try:
            safe_destination(root, url)
            git(["push", "--force", "--", url, "{}:refs/heads/{}".format(commit, args.branch)], root)
        except ToolError as exc:
            raise ToolError("Main repo push succeeded, but Vogsphere deployment failed. "
                            "Fix the destination/access problem and retry deploy.\n{}".format(exc))
        say("Deployed {} to remote branch {}.".format(args.project, args.branch), "success")


def command_status(root: Path, check_remote: bool, project: Optional[str] = None) -> None:
    """Show repository state and compare project splits with remote master."""
    project = project or (active_project(root) if active_project(root) in registered_groups(root) else None)
    if project:
        project_directory(root, project)
    print("Main repo:  {}".format(root))
    print("Project:    {}".format(project or active_project(root) or "(main repo root)"))
    print("Branch:     {}".format(current_branch(root) or "(detached HEAD)"))
    print("\nWorking tree:")
    status_args = ["status", "--short", "--branch"]
    if project:
        status_args.extend(["--", ":(literal)" + project])
    print(git(status_args, root).stdout.rstrip() or "(clean)")
    groups = registered_groups(root)
    for name in sorted(groups):
        if not project or project == name:
            try:
                print(group_status(root, name))
            except ToolError as exc:
                print("Group {}: {}".format(name, exc))
    config = load_config(root)
    if project:
        config = {project: config[project]} if project in config else {}
    print("\nVogsphere synchronization (remote branch: master):")
    if not config:
        print("  No project remote configured for {}.".format(project) if project else "  No project remotes configured.")
        return
    if not check_remote:
        for project, url in sorted(config.items()):
            print("  {}: {} (not checked)".format(project, url))
        print("  Use '{} status {}--remote' to check synchronization.".format(PROG, project + " " if project else ""))
        return
    failures: List[str] = []
    for project, url in sorted(config.items()):
        try:
            project_directory(root, project)
            require_head(root)
            group = group_repository(root, project)
            local = git(["rev-parse", "HEAD"], group).stdout.strip() if group else split_project(root, project)
            remote = remote_head(root, url, "master")
            if remote is None:
                state = "remote master does not exist"
            elif local == remote:
                state = "in sync"
            else:
                state = "different (local {}, remote {})".format(local[:12], remote[:12])
            print("  {}: {}".format(project, state))
        except ToolError as exc:
            failures.append(project)
            print("  {}: unavailable ({})".format(project, exc))
    if failures:
        raise ToolError("Could not check: {}.".format(", ".join(failures)))


def command_project_git(args: argparse.Namespace, root: Path) -> None:
    """Run a supported Git operation restricted to one top-level project."""
    project = args.project or active_project(root)
    if not project:
        raise ToolError("Specify a project at the repository root.")
    project_directory(root, project)
    group = group_repository(root, project)
    command = args.command
    options = []
    if command == "add":
        options = ["-A"]
    elif command == "log":
        options = ["--oneline"]
    elif command == "diff" and args.cached:
        options = ["--cached"]
    elif command == "restore" and args.staged:
        options = ["--staged"]
    pathspec = [] if group and command == "log" else ["--", "." if group else ":(literal)" + project]
    result = git([command, *options, *pathspec], group or root)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")


def destination_key(root: Path, url: str) -> str:
    """Compare common URL spellings, honoring Git's local URL rewrites."""
    url = git(["ls-remote", "--get-url", "--", url], root).stdout.strip()
    # Drive-letter paths are local destinations, not SCP-style host:path URLs.
    if re.match(r"^[A-Za-z]:[/\\]", url) or url.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(url))
    if "://" in url:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise ToolError("Invalid remote URL: {}".format(exc))
        if parsed.scheme != "file":
            host = (parsed.hostname or "").lower()
            default_ports = {"ssh": 22, "https": 443, "http": 80, "git": 9418}
            try:
                port = parsed.port
            except ValueError as exc:
                raise ToolError("Invalid remote URL: {}".format(exc))
            if port is not None and port != default_ports.get(parsed.scheme):
                host += ":{}".format(port)
            return "{}:{}".format(host, parsed.path.lstrip("/").rstrip("/").removesuffix(".git"))
        url = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return ntpath.normcase(ntpath.normpath("//" + parsed.netloc + url))
        if re.match(r"^/[A-Za-z]:/", url):
            return ntpath.normcase(ntpath.normpath(url[1:]))
    elif re.match(r"^(?:[^/@:]+@)?[^/:]+:", url):
        host, path = url.split(":", 1)
        return "{}:{}".format(host.split("@")[-1].lower(), path.lstrip("/").rstrip("/").removesuffix(".git"))
    return os.path.normcase(str((root / url).resolve()))


def repository_destinations(root: Path) -> set:
    """Protect every configured fetch/push URL and the local repository itself."""
    result = {destination_key(root, str(root)), destination_key(root, git(["rev-parse", "--absolute-git-dir"], root).stdout.strip())}
    for remote in git(["remote"], root).stdout.splitlines():
        for mode in ([], ["--push"]):
            for url in git(["remote", "get-url", "--all", *mode, remote], root).stdout.splitlines():
                result.add(destination_key(root, url))
    return result


def group_clone_url(root: Path, url: str) -> str:
    """Resolve relative .gitmodules URLs against the parent's default remote."""
    if not url.startswith(("./", "../")):
        return url
    branch = current_branch(root)
    try:
        remote = git(["config", "--get", "branch.{}.remote".format(branch)], root).stdout.strip() if branch else "origin"
    except ToolError:
        remote = "origin"
    try:
        base = git(["remote", "get-url", remote], root).stdout.strip() if remote != "." else str(root)
    except ToolError:
        base = str(root)
    if "://" in base:
        try:
            parsed = urlsplit(base)
        except ValueError as exc:
            raise ToolError("Invalid parent remote URL: {}".format(exc))
        return parsed._replace(path=posixpath.normpath(parsed.path + "/" + url)).geturl()
    if re.match(r"^(?:[^/@:]+@)?[^/:]+:", base) and not ntpath.splitdrive(base)[0]:
        host, path = base.split(":", 1)
        return host + ":" + posixpath.normpath(path + "/" + url)
    return os.path.normpath(os.path.join(base, url))


def safe_destination(root: Path, url: str, group: Optional[Path] = None) -> None:
    """Apply rewrite checks in both repository contexts (including push rewrites)."""
    protected = repository_destinations(root)
    if group:
        protected.update(repository_destinations(group))
        clone_url = group_clone_url(root, registered_groups(root)[group.name])
        protected.update(destination_key(context, clone_url) for context in (root, group))
    for context in (root, group) if group else (root,):
        candidates = {destination_key(context, url)}
        if url in git(["remote"], context).stdout.splitlines():
            candidates.update(destination_key(context, item) for item in
                              git(["remote", "get-url", "--push", "--all", url], context).stdout.splitlines())
        # Git applies the longest pushInsteadOf prefix to literal push URLs.
        rules = git(["config", "--null", "--list"], context).stdout.split("\0")
        rewrites = []
        for rule in rules:
            key, _, prefix = rule.partition("\n")
            if key.startswith("url.") and key.endswith(".pushinsteadof") and url.startswith(prefix):
                rewrites.append((len(prefix), key[4:-14] + url[len(prefix):]))
        if rewrites:
            candidates.add(destination_key(context, max(rewrites, key=lambda rule: rule[0])[1]))
        if candidates & protected:
            raise ToolError("Deployment points to the main repo or group collaboration repository; use a separate Vogsphere destination.")


def preflight_repository(root: Path, push: bool = False, own_index_lock: bool = False, identity: bool = True) -> None:
    """Check local write prerequisites without changing refs, branches or files."""
    require_head(root)
    if current_branch(root) is None:
        raise ToolError("HEAD is detached in {}. Select a branch with 'git -C {} switch <branch>' first.".format(root, root))
    reference = git(["symbolic-ref", "HEAD"], root).stdout.strip()
    if not reference.startswith("refs/heads/"):
        raise ToolError("HEAD must reference a local branch in {}.".format(root))
    # var checks effective identity, including Git's environment overrides.
    if identity:
        git(["var", "GIT_AUTHOR_IDENT"], root)
        git(["var", "GIT_COMMITTER_IDENT"], root)
    index_entries(root)
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG", "index.lock", reference + ".lock"):
        if marker == "index.lock" and own_index_lock:
            continue
        path = Path(git(["rev-parse", "--git-path", marker], root).stdout.strip())
        if (root / path).exists():
            raise ToolError("Git operation in progress ({}) in {}; finish it before continuing.".format(marker, root))
    if push:
        try:
            upstream(root)
        except ToolError as exc:
            raise ToolError("{}: {}".format(root, exc))
        branch = current_branch(root)
        remote = git(["config", "--get", "branch.{}.remote".format(branch)], root).stdout.strip()
        merge = git(["config", "--get", "branch.{}.merge".format(branch)], root).stdout.strip()
        if not remote or remote.startswith("-") or not merge.startswith("refs/heads/"):
            raise ToolError("Invalid upstream configuration in {}.".format(root))
        if remote != ".":
            git(["remote", "get-url", "--push", remote], root)


def pointers(root: Path, project: str) -> Tuple[str, str]:
    """Return the committed and indexed parent gitlink object IDs."""
    entry = index_entries(root).get(project)
    if not entry or entry[0] != "160000":
        raise ToolError("Missing index gitlink for {}.".format(project))
    line = git(["ls-tree", "HEAD", "--", ":(literal)" + project], root).stdout
    if not line.startswith("160000 commit "):
        raise ToolError("Missing committed parent pointer for {}.".format(project))
    return line.split()[2], entry[1]


def preflight_pointer(root: Path, project: str, target: str, allow_target: bool = False) -> None:
    committed, staged = pointers(root, project)
    if staged != committed and not (allow_target and staged == target):
        raise ToolError("An already-staged group pointer for {} would be overwritten. Review 'git diff --cached -- {}' and explicitly unstage it before retrying.".format(project, project))
    for kind in ([], ["--cached"]):
        if git(["diff", *kind, "--name-only", "--", ".gitmodules"], root).stdout.strip():
            raise ToolError("Commit or restore .gitmodules registration changes explicitly before pinning a group.")


def unpublished_warning(project: str) -> None:
    say("Warning: group commits may be unpublished. Publishing the parent alone could reference an unavailable group commit. Push {} to its collaboration upstream FIRST, then push the parent.".format(project), "warning")


def group_status(root: Path, project: str) -> str:
    committed, staged = pointers(root, project)
    deployment = load_config(root).get(project) or "missing; run vogsync remote set {} <Vogsphere URL>".format(project)
    try:
        group = group_repository(root, project)
    except ToolError as exc:
        return ("\nGroup {}: {}\n  Parent committed pointer: {}\n  Parent index pointer: {}\n"
                "  Group HEAD/branch/upstream/internal changes: unavailable\n  Deployment: {}".format(project, exc, committed, staged, deployment))
    assert group is not None
    head = git(["rev-parse", "HEAD"], group).stdout.strip()
    try:
        tracking = upstream(group)
        comparison = git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], group).stdout.split()
        tracking += " (cached: {} ahead, {} behind)".format(*comparison)
    except ToolError as exc:
        tracking = str(exc)
    return ("\nGroup {}: initialized\n  Branch: {}\n  Upstream: {}\n"
            "  Parent committed pointer: {}\n  Parent index pointer: {}{}\n"
            "  Group HEAD: {}{}\n  Deployment: {}\n  Inside group (XY = staged/unstaged):\n{}".format(
                project, current_branch(group) or "detached HEAD; select a branch with git switch <branch>", tracking,
                committed, staged, " (staged pointer change)" if staged != committed else "",
                head, " (not recorded in parent; run vogsync pin {})".format(project) if head != committed else " (recorded)",
                deployment,
                git(["--no-optional-locks", "status", "--short"], group).stdout.rstrip() or "  (clean)"))


def pin_group(root: Path, project: str, expected: Optional[str] = None) -> bool:
    """Build a pointer-only tree with hooks, then CAS the branch and replace only
    the gitlink in a copy of the real index. Hold index.lock throughout; never
    reset the real index or expose unrelated staged entries to commit hooks.
    """
    group = group_repository(root, project)
    if group is None:
        raise ToolError("pin requires a registered group repository.")
    preflight_repository(root)
    preflight_repository(group)
    target = git(["rev-parse", "HEAD"], group).stdout.strip()
    group_branch = current_branch(group)
    if expected and target != expected:
        raise ToolError("Group HEAD changed after commit; review it before pinning.")
    preflight_pointer(root, project, target, allow_target=True)
    old = git(["rev-parse", "HEAD"], root).stdout.strip()
    branch = git(["symbolic-ref", "HEAD"], root).stdout.strip()
    committed, staged = pointers(root, project)
    if committed == target and staged == target:
        say("Group {} is already recorded; nothing to pin.".format(project), "success")
        return False
    index = Path(git(["rev-parse", "--git-path", "index"], root).stdout.strip())
    index = root / index
    lock = index.with_name(index.name + ".lock")
    try:
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ToolError("Parent index is locked; no pointer commit created.")
    lock_stat = os.fstat(fd)
    os.close(fd)
    lock_owned = True

    def owns_lock() -> bool:
        try:
            current = lock.lstat()
            return (current.st_dev, current.st_ino) == (lock_stat.st_dev, lock_stat.st_ino)
        except FileNotFoundError:
            return False

    try:
        original_index = index.read_bytes()
        with tempfile.TemporaryDirectory(prefix="vogsync-pin-", dir=str(index.parent)) as directory:
            temporary = Path(directory)
            isolated = temporary / "index"
            env = dict(os.environ, GIT_INDEX_FILE=str(isolated))
            git(["read-tree", old], root, env=env)
            git(["update-index", "--add", "--cacheinfo", "160000,{},{}".format(target, project)], root, env=env)
            tree = git(["write-tree"], root, env=env).stdout.strip()
            message = temporary / "message"
            message.write_text("chore({}): update group repository revision\n".format(project), encoding="utf-8")
            hook_env = dict(env, GIT_EDITOR=":")
            for hook, arguments in (("pre-commit", []), ("prepare-commit-msg", [str(message), "message"]), ("commit-msg", [str(message)])):
                git(["hook", "run", "--ignore-missing", hook, "--", *arguments], root, env=hook_env)
            if not message.read_text(encoding="utf-8").strip():
                raise ToolError("Parent commit hook emptied the commit message; no parent commit created.")
            # Hooks may edit the temporary index or either repository. Do not
            # record their unexpected state or overwrite a concurrent edit.
            if git(["write-tree"], root, env=env).stdout.strip() != tree:
                raise ToolError("Parent commit hook changed the pointer-only index; no parent commit created.")
            preflight_repository(group)
            preflight_repository(root, own_index_lock=True)
            check_layout(root, registered_groups(root))
            if (git(["rev-parse", "HEAD"], group).stdout.strip() != target or
                    current_branch(group) != group_branch or
                    git(["symbolic-ref", "HEAD"], root).stdout.strip() != branch or
                    index.read_bytes() != original_index):
                raise ToolError("Repository state changed during parent hooks; no parent pointer recorded.")
            # Run operation checks again, excluding the lock owned by us.
            index_entries(root)
            preflight_pointer(root, project, target, allow_target=True)
            signing = git(["config", "--type=bool", "--default=false", "--get", "commit.gpgSign"], root).stdout.strip() == "true"
            commit = git(["commit-tree", *(["-S"] if signing else []), tree, "-p", old, "-F", str(message)], root, env=env).stdout.strip()
            preserved = temporary / "preserved-index"
            preserved.write_bytes(original_index)
            preserved_env = dict(os.environ, GIT_INDEX_FILE=str(preserved))
            git(["update-index", "--cacheinfo", "160000,{},{}".format(target, project)], root, env=preserved_env)
            if not owns_lock() or index.read_bytes() != original_index:
                raise ToolError("Parent index/lock changed during pinning; no parent pointer recorded.")
            lock.write_bytes(preserved.read_bytes())
            git(["update-ref", "-m", "vogsync: pin " + project, branch, commit, old], root)
            os.replace(str(lock), str(index))
            lock_owned = False
            say("Parent pointer committed: {} ({}).".format(project, commit), "success")
            try:
                git(["hook", "run", "--ignore-missing", "post-commit"], root, env=hook_env)
            except ToolError as exc:
                raise ToolError("Parent pointer commit {} succeeded, but post-commit hook failed. Commits retained.\n{}".format(commit, exc))
        return True
    finally:
        if lock_owned and owns_lock():
            lock.unlink()


def command_pin(args: argparse.Namespace, root: Path) -> None:
    project = args.project
    project_directory(root, project)
    try:
        pin_group(root, project)
    except (ToolError, OSError) as exc:
        raise ToolError("Pin failed; successful commits are retained. Inspect 'vogsync status {}', resolve the reported problem, then retry 'vogsync pin {}'.\n{}".format(project, project, exc))
    unpublished_warning(project)


def push_group_and_parent(root: Path, group: Path, pin: bool) -> None:
    group_head = git(["rev-parse", "HEAD"], group).stdout.strip()
    parent_head = git(["rev-parse", "HEAD"], root).stdout.strip()
    if pin and pointers(root, group.name) != (group_head, group_head):
        raise ToolError("Parent pointers no longer match group HEAD; nothing pushed. Review 'vogsync status {}' and recover with 'vogsync pin {}'.".format(group.name, group.name))
    try:
        push_upstream(group)
    except ToolError as exc:
        raise ToolError("Group push failed; parent and Vogsphere were not pushed. Local commits retained.\n{}".format(exc))
    say("Group collaboration upstream pushed.", "success")
    if not pin:
        say("--no-pin: only the group was pushed; the parent pointer remains unrecorded.", "warning")
        return
    if (git(["rev-parse", "HEAD"], group).stdout.strip() != group_head or
            git(["rev-parse", "HEAD"], root).stdout.strip() != parent_head or
            pointers(root, group.name) != (group_head, group_head)):
        raise ToolError("Group push succeeded, but repository state changed during push; parent and Vogsphere were not pushed. Review 'vogsync status {}' before retrying.".format(group.name))
    try:
        push_upstream(root)
    except ToolError as exc:
        raise ToolError("Group push succeeded, but parent push failed; Vogsphere was not updated. Local commits retained. Review and retry the parent push.\n{}".format(exc))
    say("Main repo pushed to {}.".format(upstream(root)), "success")


def command_group_commit(args: argparse.Namespace, root: Path, project: str) -> None:
    project_directory(root, project)
    group = group_repository(root, project)
    if not args.message.strip():
        raise ToolError("Commit message must not be empty.")
    preflight_repository(group, args.push)
    if not args.no_pin:
        preflight_repository(root, args.push)
        preflight_pointer(root, project, git(["rev-parse", "HEAD"], group).stdout.strip())
    before = git(["rev-parse", "HEAD"], group).stdout.strip()
    branch = current_branch(group)
    try:
        result = git(["commit", *(["--all"] if args.all else []), "-m", args.message], group)
        print(result.stdout.strip())
        target = git(["rev-parse", "HEAD"], group).stdout.strip()
        if current_branch(group) != branch or git(["rev-parse", "HEAD^"], group).stdout.strip() != before:
            raise ToolError("Group hooks changed branch/history; review before pinning or pushing.")
        if not args.no_pin:
            pin_group(root, project, expected=target)
    except (ToolError, OSError) as exc:
        now = git(["rev-parse", "HEAD"], group).stdout.strip()
        if now != before:
            try:
                committed, staged = pointers(root, project)
            except ToolError:
                committed, staged = "unavailable (inspect parent HEAD)", "unavailable (inspect parent index)"
            raise ToolError("Group HEAD is now {} (was {}); successful commits are retained and may be unpublished. Parent committed pointer: {}; parent index pointer: {}. Inspect 'vogsync status {}', then recover with 'vogsync pin {}'. Push the group before publishing the parent.\n{}".format(now, before, committed, staged, project, project, exc))
        raise
    if args.push:
        push_group_and_parent(root, group, not args.no_pin)
    else:
        if args.no_pin:
            say("--no-pin: group committed; parent pointer remains unrecorded. Run 'vogsync pin {}'.".format(project), "warning")
        unpublished_warning(project)


def command_group_deploy(args: argparse.Namespace, root: Path) -> None:
    if args.pull:
        raise ToolError("--pull is unsupported for groups. Update both repositories explicitly, select a group branch, and pin its reviewed HEAD before deploying.")
    group = group_repository(root, args.project)
    preflight_repository(root, True, identity=False)
    preflight_repository(group, True, identity=False)
    target = git(["rev-parse", "HEAD"], group).stdout.strip()
    if pointers(root, args.project) != (target, target):
        raise ToolError("Parent committed/index pointers must match group HEAD. Review changes, then run 'vogsync pin {}'.".format(args.project))
    for repository in (root, group):
        if git(["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"], repository).stdout.strip():
            raise ToolError("Working tree is dirty in {}. Commit or stash explicitly before deploying.".format(repository))
    url = configured_url(load_config(root), args.project)
    check_url(url)
    git(["check-ref-format", "refs/heads/" + args.branch], root)
    safe_destination(root, url, group)
    if args.dry_run:
        remote_head(group, url, args.branch)
        say("Dry run successful: group history {} selected; nothing pushed or changed.".format(target), "success")
        return
    push_group_and_parent(root, group, True)
    # A push hook can modify either checkout. Never deploy a newly selected HEAD.
    if (git(["rev-parse", "HEAD"], group).stdout.strip() != target or
            pointers(root, args.project) != (target, target) or
            any(git(["status", "--porcelain", "--ignore-submodules=none"], repo).stdout.strip() for repo in (root, group))):
        raise ToolError("Group and parent pushes succeeded, but local state changed during push; Vogsphere was not updated. Review before retrying.")
    try:
        safe_destination(root, url, group)
        git(["push", "--force", "--", url, "{}:refs/heads/{}".format(target, args.branch)], group)
    except ToolError as exc:
        raise ToolError("Group and main repo pushes succeeded, but Vogsphere deployment failed. Fix the destination/access problem and retry deploy.\n{}".format(exc))
    say("Deployed {} directly to Vogsphere branch {}.".format(args.project, args.branch), "success")


def command_doctor(root: Path, check_remote: bool, target_branch: str) -> bool:
    """Diagnose setup without changing files, refs, or fetching remote objects."""
    counts = {"OK": 0, "WARN": 0, "ERROR": 0}

    def report(state: str, message: str) -> None:
        counts[state] += 1
        say("[{}] {}".format(state, message), {"OK": "success", "WARN": "warning", "ERROR": "error"}[state])

    def check_git(label: str, arguments: Sequence[str]) -> Optional[str]:
        try:
            output = git(["--no-optional-locks", *arguments], root).stdout.strip()
            report("OK", label)
            return output
        except ToolError as exc:
            report("ERROR", "{}: {}".format(label, exc))
            return None

    print("Main repo: {}\n".format(root))
    check_git("Git available", ["--version"])
    # The help command does not run a split or create Git objects.
    probe = subprocess.run(["git", "subtree", "-h"], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if probe.returncode in (0, 129) and "git subtree" in probe.stdout + probe.stderr:
        report("OK", "git subtree available")
    else:
        report("ERROR", "git subtree is unavailable; install Git's subtree extension.")
    head = check_git("HEAD resolves to a commit (otherwise create an initial commit)", ["rev-parse", "--verify", "HEAD^{commit}"])
    check_git("Git object integrity", ["fsck", "--full", "--no-dangling"])
    for field in ("name", "email"):
        try:
            value = git(["config", "--get", "user." + field], root).stdout.strip()
        except ToolError:
            value = ""
        report("OK" if value else "WARN", "Git user.{} {}".format(field, "is configured: " + value if value else "is missing; set it with git config user.{} <value>".format(field)))

    branch = current_branch(root)
    if branch is None:
        report("WARN", "Detached HEAD; check out a branch before using --push or --pull.")
    else:
        report("OK", "Current branch: {}".format(branch))
        try:
            tracking = upstream(root)
            report("OK", "Upstream: {}".format(tracking))
            counts_text = git(["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], root).stdout.split()
            ahead, behind = map(int, counts_text)
            report("WARN" if ahead or behind else "OK", "Upstream comparison (cached, no fetch): {} ahead, {} behind.{}".format(ahead, behind, " Pull/push after reviewing changes." if ahead or behind else ""))
        except ToolError as exc:
            report("WARN", str(exc))
    dirty = check_git("Working-tree status readable", ["status", "--porcelain"])
    if dirty is not None:
        report("WARN" if dirty else "OK", "Uncommitted changes; commit or stash before deploy." if dirty else "Working tree is clean.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG"):
        path = git(["rev-parse", "--git-path", marker], root).stdout.strip()
        if (root / path).exists():
            report("WARN", "Git operation in progress ({}); finish or abort it before deploying.".format(marker))

    # Ask Git for paths; a linked worktree's .git is a file, not a directory.
    try:
        groups = registered_groups(root)
    except ToolError as exc:
        report("ERROR", str(exc))
        groups = {}
    def scan_error(exc: OSError) -> None:
        report("ERROR", "Could not inspect project layout: {}".format(exc))

    nested = []
    for directory, directories, files in os.walk(root, onerror=scan_error, followlinks=False):
        path = Path(directory)
        if path != root and (".git" in directories or ".git" in files):
            if path.relative_to(root).as_posix() not in groups:
                nested.append(str(path.relative_to(root)))
        if ".git" in directories:
            directories.remove(".git")
    for path in sorted(nested):
        report("ERROR", "Nested Git repository at {}; projects must belong to the main repo, not separate repositories.".format(path))
    if not nested:
        report("OK", "No nested Git repositories.")
    index = check_git("Git index readable", ["ls-files", "--stage", "-z"])
    if index:
        for entry in index.split("\x00"):
            if entry.startswith("160000 ") and entry.split("\t", 1)[1] not in groups:
                report("ERROR", "Submodule/gitlink at {}; subtree deployment does not include its files. Use a regular project directory.".format(entry.split("\t", 1)[1]))
            if entry and entry.split("\t", 1)[0].split()[-1] != "0":
                report("ERROR", "Unresolved merge conflict at {}; resolve and commit before deploying.".format(entry.split("\t", 1)[1]))

    config_path = root / CONFIG_FILE_NAME
    config = None
    excluded = []
    try:
        config, excluded = load_settings(root)
        report("OK", "Project configuration is valid.")
        if not config:
            report("WARN", "No project Vogsphere URLs configured in {}; this is separate from main repo remotes. Use '{} remote set PROJECT URL'.".format(CONFIG_FILE_NAME, PROG))
    except ToolError as exc:
        report("ERROR", str(exc))
    if config_path.is_symlink():
        report("ERROR", "{} is a symlink; use a regular configuration file.".format(CONFIG_FILE_NAME))
    if config_path.exists():
        report("OK", "Project configuration file exists.")
        # Git does not preserve restrictive permissions on tracked files.
        # Windows access is controlled by ACLs, not POSIX permission bits.
        tracked = git(["ls-files", "--", CONFIG_FILE_NAME], root).stdout.strip()
        if not tracked and os.name != "nt" and config_path.stat().st_mode & 0o077:
            report("WARN", "{} is accessible to other users; consider chmod 600 {}.".format(CONFIG_FILE_NAME, CONFIG_FILE_NAME))

    git_dir = git(["rev-parse", "--absolute-git-dir"], root).stdout.strip()
    main_repo_destinations = {destination_key(root, str(root)), destination_key(root, git_dir)}
    remotes = git(["remote"], root).stdout.splitlines()
    if not remotes:
        report("WARN", "No main repo Git remote; add one before pushing your work.")
    for remote in remotes:
        try:
            for mode in ([], ["--push"]):
                urls = git(["remote", "get-url", "--all", *mode, remote], root).stdout.splitlines()
                main_repo_destinations.update(destination_key(root, url) for url in urls)
            report("OK", "Main repo remote {} is configured.".format(remote))
            if check_remote:
                url = git(["remote", "get-url", remote], root).stdout.strip()
                remote_head(root, url, target_branch)
                report("OK", "Main repo remote {} is readable.".format(remote))
        except (ToolError, ValueError) as exc:
            report("ERROR", "Main repo remote {}: {}".format(remote, exc))

    projects = set(config or {}) | set(groups)
    if head:
        tree = check_git("Top-level committed layout readable", ["ls-tree", "-z", "--name-only", "-d", "HEAD"])
        projects.update(name for name in (tree or "").split("\x00") if name and not name.startswith("."))
    # Include new project directories even before their first commit, but not
    # ignored build/cache directories. Never follow symlinks during discovery.
    for path in root.iterdir():
        if not path.name.startswith(".") and path.is_dir():
            ignored = subprocess.run(["git", "check-ignore", "-q", "--", path.name],
                                     cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
            if ignored.returncode == 1:
                projects.add(path.name)
    for project in sorted(excluded):
        report("OK", "{}: excluded from project diagnosis.".format(project))
    destinations: Dict[str, str] = {}
    for project in sorted(projects - set(excluded)):
        try:
            project_directory(root, project)
            if project in groups:
                report("OK", "{}: registered top-level group repository (clone URL: {}).".format(project, groups[project]))
                report("OK", group_status(root, project))
                group = group_repository(root, project)
                try:
                    upstream(group)
                except ToolError as exc:
                    report("WARN", "{}: {}".format(project, exc))
                if git(["--no-optional-locks", "status", "--porcelain"], group).stdout.strip():
                    report("WARN", "{}: changes inside the group; inspect vogsync diff {} (and --cached).".format(project, project))
                committed_pointer, index_pointer = pointers(root, project)
                if committed_pointer != index_pointer:
                    report("WARN", "{}: parent index has a staged pointer change, separate from group file changes.".format(project))
                try:
                    preflight_repository(group)
                except ToolError as exc:
                    report("ERROR", "{}: {}".format(project, exc))
                if pointers(root, project)[0] != git(["rev-parse", "HEAD"], group).stdout.strip():
                    report("WARN", "{}: group HEAD is not recorded in parent; review then run vogsync pin {}.".format(project, project))
                if check_remote:
                    for remote in git(["remote"], group).stdout.splitlines():
                        remote_head(group, git(["remote", "get-url", remote], group).stdout.strip(), target_branch)
                        report("OK", "{}: collaboration remote {} is readable.".format(project, remote))
            else:
                report("OK", "{}: regular top-level project directory.".format(project))
            if head and project not in groups:
                kind = git(["cat-file", "-t", "HEAD:" + project], root).stdout.strip()
                if kind != "tree":
                    raise ToolError("Project is not a committed directory; commit its files before deploying.")
                report("OK", "{}: committed project tree exists.".format(project))
        except ToolError as exc:
            report("ERROR", "{}: {}".format(project, exc))
        url = (config or {}).get(project)
        if not url:
            if config is not None:
                report("WARN", "{}: no project Vogsphere URL configured; use '{} remote set {} URL'.".format(project, PROG, project))
            continue
        try:
            key = destination_key(root, url)
            if project in groups:
                safe_destination(root, url, group_repository(root, project))
            if key in main_repo_destinations:
                report("ERROR", "{}: deployment points to the main repo! Set a separate Vogsphere URL; deploy force-pushes.".format(project))
            if key in destinations:
                report("ERROR", "{} and {} share a deployment destination; use a separate repository per project.".format(destinations[key], project))
            destinations[key] = project
            if check_remote:
                sha = remote_head(root, url, target_branch)
                report("OK" if sha else "WARN", "{}: remote is readable; {}.".format(project, "branch {} exists".format(target_branch) if sha else "branch {} does not exist yet".format(target_branch)))
        except (ToolError, ValueError) as exc:
            report("ERROR", "{}: {}".format(project, exc))
    if not check_remote:
        print("\nOffline checks only; use '{} doctor --remote' to check remote read access.".format(PROG))
    else:
        print("\nRemote checks verify read access only, not push permissions or synchronization.")
    print("\nDiagnosis: {} OK, {} warning(s), {} error(s).".format(counts["OK"], counts["WARN"], counts["ERROR"]))
    return counts["ERROR"] == 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI with its fixed command name."""
    parser = argparse.ArgumentParser(prog=PROG, description="Manage project-specific Vogsphere deployments from a main repo.")
    parser.add_argument("-v", "--version", action="version", version="%(prog)s " + VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    remote = commands.add_parser("remote", help="Manage project remote URLs.")
    remotes = remote.add_subparsers(dest="remote_command", required=True)
    remote_set = remotes.add_parser("set", help="Set a project's remote URL.")
    remote_set.add_argument("project")
    remote_set.add_argument("url")
    remote_get = remotes.add_parser("get", help="Print a project's remote URL.")
    remote_get.add_argument("project")
    remotes.add_parser("list", help="List configured remotes.")
    commit = commands.add_parser("commit", help="Commit a regular project or group, automatically pinning groups.",
                                 description="Regular projects commit the parent index (with -a: tracked changes in the project only). Groups commit their own index (with -a: tracked changes only), then create a separate pointer-only parent commit. Unrelated parent changes are preserved; successful commits are never rolled back.")
    commit.add_argument("project", nargs="?", help="Project name; defaults to cwd.")
    commit.add_argument("-m", "--message", required=True, help="Commit message.")
    commit.add_argument("-a", "--all", action="store_true", help="Include tracked modifications and deletions, limited to the active project when applicable.")
    commit.add_argument("-p", "--push", action="store_true", help="Push normally to configured upstreams: group first, then parent (group only with --no-pin).")
    commit.add_argument("--no-pin", action="store_true", help="Group only: commit without recording the parent pointer; --push pushes only the group.")
    pin = commands.add_parser("pin", help="Commit only a registered group's current pointer in the parent (no push).")
    pin.add_argument("project", help="Registered top-level group name; already recorded is a safe no-op.")
    deploy = commands.add_parser("deploy", help="Push the main repo to its upstream, then deploy a project.",
                                 description="Push group (if applicable), then main repo upstreams without force; force-push project history only to Vogsphere. Groups require clean branches/upstreams and a matching committed parent pointer; --pull is unsupported for groups.")
    deploy.add_argument("project")
    deploy.add_argument("--pull", action="store_true", help="Regular projects only: fast-forward parent first, even in dry runs. Rejected for groups.")
    deploy.add_argument("--dry-run", action="store_true", help="Select committed history and check remote read connectivity without pushing. Groups never update branches/pointers.")
    deploy.add_argument("--branch", default="master", help="Target remote branch (default: master).")
    status = commands.add_parser("status", help="Show repository status and configured remotes.")
    status.add_argument("project", nargs="?", help="Show only this project's changes and remote (default: whole repo).")
    status.add_argument("-r", "--remote", action="store_true", help="Check synchronization with Vogsphere remotes (requires network).")
    for name, help_text in (("add", "Stage all changes in a project."),
                            ("diff", "Show unstaged changes in a project."),
                            ("log", "Show commits affecting a project."),
                            ("restore", "Restore unstaged changes in a project.")):
        project_command = commands.add_parser(name, help=help_text)
        project_command.add_argument("project", nargs="?", help="Project name; defaults to cwd (required at repo root).")
        if name == "diff":
            project_command.add_argument("--cached", action="store_true", help="Show staged changes instead.")
        if name == "restore":
            project_command.add_argument("--staged", action="store_true", help="Unstage project changes instead.")
    doctor = commands.add_parser("doctor", help="Diagnose Git health, project layout, and deployment configuration (read-only).")
    doctor.add_argument("-r", "--remote", action="store_true", help="Also check read access to main repo and project remotes (requires network).")
    doctor.add_argument("--branch", default="master", help="Project remote branch to check (default: master).")
    return parser


def main() -> int:
    """Dispatch a command and convert expected failures to exit codes."""
    args = build_parser().parse_args()
    try:
        root = repository_root()
        if args.command == "remote":
            command_remote(args, root)
        elif args.command == "commit":
            command_commit(args, root)
        elif args.command == "pin":
            command_pin(args, root)
        elif args.command == "deploy":
            command_deploy(args, root)
        elif args.command == "status":
            command_status(root, args.remote, args.project)
        elif args.command in ("add", "diff", "log", "restore"):
            command_project_git(args, root)
        elif args.command == "doctor":
            if not args.branch or args.branch.startswith("-"):
                raise ToolError("Invalid target branch: {!r}".format(args.branch))
            git(["check-ref-format", "refs/heads/" + args.branch], root)
            return 0 if command_doctor(root, args.remote, args.branch) else 1
        return 0
    except ToolError as exc:
        say("{}: error: {}".format(PROG, exc), "error", error=True)
        return 1
    except KeyboardInterrupt:
        say("{}: interrupted".format(PROG), "warning", error=True)
        return 1
    except OSError as exc:
        say("{}: error: {}".format(PROG, exc), "error", error=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
