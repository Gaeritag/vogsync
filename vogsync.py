#!/usr/bin/env python3
"""Manage project-specific Vogsphere deployments from a main repo."""

CLI_NAME = "vogsync"
CONFIG_FILE_NAME = ".vogsync.json"

import argparse
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
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


def git(args: Sequence[str], root: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run Git without a shell and provide a useful error on failure."""
    try:
        return subprocess.run(["git", *args], cwd=str(root) if root else None,
                              check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise ToolError("Git was not found. Install Git and make sure it is in PATH.")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or "Git exited with status {}.".format(exc.returncode)
        raise ToolError("git {} failed:\n{}".format(" ".join(args), detail))
    except OSError as exc:
        raise ToolError("Could not run Git: {}".format(exc))


def repository_root() -> Path:
    """Resolve the repository root, including worktrees and submodules."""
    return Path(git(["rev-parse", "--show-toplevel"]).stdout.strip()).resolve()


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
    if path.is_symlink() or not path.is_dir():
        raise ToolError("Project must be an existing, non-symlink directory: {}".format(path))
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
    """Read the local project-to-URL mapping."""
    return load_settings(root)[0]


def ensure_config_ignored(root: Path) -> None:
    """Add an exact root-level ignore rule unless one already applies."""
    try:
        subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", CONFIG_FILE_NAME],
                       cwd=str(root), check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 1:
            raise ToolError("Could not check ignore rules: {}".format((exc.stderr or "git check-ignore failed").strip()))
    except OSError as exc:
        raise ToolError("Could not check ignore rules: {}".format(exc))
    path = root / ".gitignore"
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        with path.open("a", encoding="utf-8") as handle:
            handle.write(("\n" if existing and not existing.endswith("\n") else "") + "/" + CONFIG_FILE_NAME + "\n")
    except OSError as exc:
        raise ToolError("Could not update {}: {}".format(path, exc))
    say("Added {} to .gitignore; review and commit the change.".format(CONFIG_FILE_NAME), "warning")
    # An ignore rule does not stop Git from tracking an already indexed file.
    tracked = git(["ls-files", "--", CONFIG_FILE_NAME], root).stdout.strip()
    if tracked:
        say("{} is already tracked; remove it from the index to keep it local.".format(CONFIG_FILE_NAME), "warning")


def save_config(root: Path, config: Dict[str, str]) -> None:
    """Save the local configuration with restrictive permissions on creation."""
    _, excluded = load_settings(root)
    if set(excluded) & config.keys():
        raise ToolError("Remove the project from exclude before configuring its Vogsphere URL.")
    settings: Dict[str, object] = dict(config)
    if excluded:
        settings["exclude"] = excluded
    ensure_config_ignored(root)
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
    """Manage local project remote mappings."""
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
    project = args.project or active_project(root)
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


def command_render(args: argparse.Namespace, root: Path) -> None:
    """Split a project and deploy the resulting commit directly."""
    project_directory(root, args.project)
    url = configured_url(load_config(root), args.project)
    check_url(url)
    if not args.branch or args.branch.startswith("-"):
        raise ToolError("Invalid target branch: {!r}".format(args.branch))
    git(["check-ref-format", "refs/heads/" + args.branch], root)
    require_head(root)
    if git(["status", "--porcelain"], root).stdout.strip():
        raise ToolError("Working tree is dirty. Commit or stash changes before rendering.")
    if args.pull:
        upstream(root)
        git(["pull", "--ff-only"], root)
        say("Updated from the configured upstream.", "success")
        project_directory(root, args.project)
    say("Splitting committed history for {}...".format(args.project))
    commit = split_project(root, args.project)
    if args.dry_run:
        remote_head(root, url, args.branch)
        say("Dry run successful: split {} and reached the remote; nothing pushed.".format(commit), "success")
    else:
        git(["push", "--force", "--", url, "{}:refs/heads/{}".format(commit, args.branch)], root)
        say("Deployed {} to remote branch {}.".format(args.project, args.branch), "success")


def command_status(root: Path, check_remote: bool) -> None:
    """Show repository state and compare project splits with remote master."""
    print("Main repo:  {}".format(root))
    print("Project:    {}".format(active_project(root) or "(main repo root)"))
    print("Branch:     {}".format(current_branch(root) or "(detached HEAD)"))
    print("\nWorking tree:")
    print(git(["status", "--short", "--branch"], root).stdout.rstrip() or "(clean)")
    config = load_config(root)
    print("\nVogsphere synchronization (remote branch: master):")
    if not config:
        print("  No project remotes configured.")
        return
    if not check_remote:
        for project, url in sorted(config.items()):
            print("  {}: {} (not checked)".format(project, url))
        print("  Use '{} status --remote' to check synchronization.".format(PROG))
        return
    failures: List[str] = []
    for project, url in sorted(config.items()):
        try:
            project_directory(root, project)
            require_head(root)
            local = split_project(root, project)
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


def destination_key(root: Path, url: str) -> str:
    """Compare common URL spellings, honoring Git's local URL rewrites."""
    url = git(["ls-remote", "--get-url", "--", url], root).stdout.strip()
    # Drive-letter paths are local destinations, not SCP-style host:path URLs.
    if re.match(r"^[A-Za-z]:[/\\]", url) or url.startswith("\\\\"):
        return ntpath.normcase(ntpath.normpath(url))
    if "://" in url:
        parsed = urlsplit(url)
        if parsed.scheme != "file":
            host = (parsed.hostname or "").lower()
            default_ports = {"ssh": 22, "https": 443, "http": 80, "git": 9418}
            if parsed.port is not None and parsed.port != default_ports.get(parsed.scheme):
                host += ":{}".format(parsed.port)
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
        report("WARN" if dirty else "OK", "Uncommitted changes; commit or stash before render." if dirty else "Working tree is clean.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG"):
        path = git(["rev-parse", "--git-path", marker], root).stdout.strip()
        if (root / path).exists():
            report("WARN", "Git operation in progress ({}); finish or abort it before deploying.".format(marker))

    # Ask Git for paths; a linked worktree's .git is a file, not a directory.
    def scan_error(exc: OSError) -> None:
        report("ERROR", "Could not inspect project layout: {}".format(exc))

    nested = []
    for directory, directories, files in os.walk(root, onerror=scan_error, followlinks=False):
        path = Path(directory)
        if path != root and (".git" in directories or ".git" in files):
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
            if entry.startswith("160000 "):
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
        report("ERROR", "{} is a symlink; use a regular local configuration file.".format(CONFIG_FILE_NAME))
    if config_path.exists():
        tracked = git(["ls-files", "--", CONFIG_FILE_NAME], root).stdout.strip()
        if tracked:
            report("ERROR", "{} is tracked; review its history and remove it from the index to keep URLs local.".format(CONFIG_FILE_NAME))
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", CONFIG_FILE_NAME], cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")
        if ignored.returncode == 1:
            report("WARN", "{} is not ignored; add /{} to .gitignore.".format(CONFIG_FILE_NAME, CONFIG_FILE_NAME))
        elif ignored.returncode != 0:
            report("ERROR", "Could not check configuration ignore rules: {}".format(ignored.stderr.strip()))
        elif not tracked:
            report("OK", "Local configuration is ignored and untracked.")
        # Windows access is controlled by ACLs, not POSIX permission bits.
        if os.name != "nt" and config_path.stat().st_mode & 0o077:
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

    projects = set(config or {})
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
            report("OK", "{}: regular top-level project directory.".format(project))
            if head:
                kind = git(["cat-file", "-t", "HEAD:" + project], root).stdout.strip()
                if kind != "tree":
                    raise ToolError("Project is not a committed directory; commit its files before rendering.")
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
            if key in main_repo_destinations:
                report("ERROR", "{}: deployment points to the main repo! Set a separate Vogsphere URL; render force-pushes.".format(project))
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
    commit = commands.add_parser("commit", help="Create a project-prefixed commit.")
    commit.add_argument("project", nargs="?", help="Project name; defaults to cwd.")
    commit.add_argument("-m", "--message", required=True, help="Commit message.")
    commit.add_argument("-a", "--all", action="store_true", help="Include tracked modifications and deletions, limited to the active project when applicable.")
    commit.add_argument("-p", "--push", action="store_true", help="Push to the current branch's configured upstream.")
    render = commands.add_parser("render", help="Split and deploy a project.")
    render.add_argument("project")
    render.add_argument("--pull", action="store_true", help="Fast-forward from the configured upstream first.")
    render.add_argument("--dry-run", action="store_true", help="Split and check remote connectivity without pushing.")
    render.add_argument("--branch", default="master", help="Target remote branch (default: master).")
    status = commands.add_parser("status", help="Show repository status and configured remotes.")
    status.add_argument("-r", "--remote", action="store_true", help="Check synchronization with Vogsphere remotes (requires network).")
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
        elif args.command == "render":
            command_render(args, root)
        elif args.command == "status":
            command_status(root, args.remote)
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
