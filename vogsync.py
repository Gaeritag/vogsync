#!/usr/bin/env python3
"""Manage project-specific Vogsphere deployments from a Git monorepo."""

CLI_NAME = "vogsync"
CONFIG_FILE_NAME = ".vogsync.json"

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Dict, List, Optional, Sequence

VERSION = "1.0.0"
PROG = CLI_NAME
COLORS = {"success": "\033[32m", "warning": "\033[33m", "error": "\033[31m", "info": "\033[36m"}


class ToolError(Exception):
    """An operational error suitable for display without a traceback."""


def say(message: str, level: str = "info", error: bool = False) -> None:
    """Print a message with color when standard output is a terminal."""
    if sys.stdout.isatty() and level in COLORS:
        message = COLORS[level] + message + "\033[0m"
    print(message, file=sys.stderr if error else sys.stdout)


def git(args: Sequence[str], root: Optional[Path] = None) -> subprocess.CompletedProcess:
    """Run Git without a shell and provide a useful error on failure."""
    try:
        return subprocess.run(["git", *args], cwd=str(root) if root else None,
                              check=True, capture_output=True, text=True)
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
    if not project or project in (".", "..", ".git") or any(c in project for c in ("/", "\\", "\x00")):
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


def load_config(root: Path) -> Dict[str, str]:
    """Read and validate the local project-to-URL mapping."""
    path = root / CONFIG_FILE_NAME
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolError("Could not read {}: {}".format(path, exc))
    if not isinstance(data, dict):
        raise ToolError("{} must contain a JSON object.".format(path))
    result: Dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise ToolError("{} must map project names to nonempty URLs.".format(path))
        validate_project(key)
        check_url(value)
        result[key] = value
    return result


def ensure_config_ignored(root: Path) -> None:
    """Add an exact root-level ignore rule unless one already applies."""
    try:
        subprocess.run(["git", "check-ignore", "-q", "--no-index", "--", CONFIG_FILE_NAME],
                       cwd=str(root), check=True, capture_output=True, text=True)
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
    ensure_config_ignored(root)
    path = root / CONFIG_FILE_NAME
    if path.is_symlink():
        raise ToolError("Refusing to overwrite symlink: {}".format(path))
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
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
                                cwd=str(root), check=True, capture_output=True, text=True)
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
            cwd=str(root), check=True, capture_output=True, text=True, env=env,
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
        say("At repository root: creating an unprefixed commit.", "warning")
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
    print("Repository: {}".format(root))
    print("Project:    {}".format(active_project(root) or "(repository root)"))
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


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI with its fixed command name."""
    parser = argparse.ArgumentParser(prog=PROG, description="Manage project-specific Vogsphere deployments from a monorepo.")
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
