#!/usr/bin/env python3
"""Install or update vogsync on Windows, macOS, and Linux using only Python."""

import argparse
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

WINDOWS = os.name == "nt"


def check_regular(path: Path) -> None:
    """Refuse links, junctions, and other reparse points as well as directories."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("{} must be a regular file, not a link or directory.".format(path))


def install(mode: str) -> None:
    if sys.version_info < (3, 9):
        raise ValueError("Python 3.9 or newer is required.")
    if not shutil.which("git"):
        raise ValueError("Git is required. Install Git and add it to PATH.")
    variable = "LOCALAPPDATA" if WINDOWS else "HOME"
    home = os.environ.get(variable)
    if not home:
        raise ValueError("{} is not set.".format(variable))
    directory = Path(home) / ("vogsync" if WINDOWS else ".local") / "bin"
    source = Path(__file__).resolve().with_name("vogsync.py").read_bytes()
    compile(source, "vogsync.py", "exec")
    # Keep the launcher ASCII-only, including when Python lives in a Unicode
    # user directory. Prefer the Windows Python launcher, falling back to PATH.
    launcher = ('@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
                'py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1\r\n'
                'if not errorlevel 1 (\r\n'
                '    py -3 "%~dp0vogsync.py" %*\r\n    exit /b\r\n)\r\n'
                'python "%~dp0vogsync.py" %*\r\nexit /b %errorlevel%\r\n')
    files = {"vogsync.py": source, "vogsync.bat": launcher.encode("ascii")} if WINDOWS else {"vogsync": source}
    # Do not follow a junction/symlink in the installation directory either.
    for parent in (directory, directory.parent):
        if not os.path.lexists(parent):
            continue
        info = parent.lstat()
        if parent.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Refusing installation through a link: {}".format(parent))
    for name in files:
        destination = directory / name
        if mode == "install":
            if os.path.lexists(destination):
                raise ValueError("{} already exists; no file was overwritten.".format(destination))
        elif not os.path.lexists(destination):
            raise ValueError("{} is not installed; run install.py install first.".format(destination))
        else:
            check_regular(destination)

    if mode == "install":
        directory.mkdir(parents=True, exist_ok=True)
        created = []
        try:
            for name, data in files.items():
                destination = directory / name
                with destination.open("xb") as handle:
                    created.append(destination)
                    handle.write(data)
                if not WINDOWS:
                    destination.chmod(0o755)
        except OSError:
            for destination in created:
                destination.unlink()
            raise
    else:
        # Stage and validate before replacing either installed file. Each rename
        # is atomic on the same filesystem; the pair is not a transaction.
        staged = []
        try:
            for name, data in files.items():
                fd, temporary = tempfile.mkstemp(prefix=".vogsync-update-", dir=directory)
                staged.append((Path(temporary), directory / name))
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                if not WINDOWS:
                    Path(temporary).chmod(0o755)
            for temporary, destination in staged:
                check_regular(destination)
                os.replace(temporary, destination)
        finally:
            for temporary, _ in staged:
                if temporary.exists():
                    temporary.unlink()
    print("{}: {}".format("Installed" if mode == "install" else "Updated", directory))
    if WINDOWS:
        print("Add this directory to your user PATH, then open a new terminal:\n  {}".format(directory))
    elif str(directory) not in os.environ.get("PATH", "").split(os.pathsep):
        print('Add this line to your shell configuration, then reload it:\n  export PATH="$HOME/.local/bin:$PATH"')


def main() -> int:
    parser = argparse.ArgumentParser(description="Install/update vogsync in ~/.local/bin (Linux/macOS) or %LOCALAPPDATA%\\vogsync\\bin (Windows). Run git pull --ff-only before updating.")
    parser.add_argument("mode", nargs="?", default="install", choices=("install", "update"),
                        help="Operation to perform (default: install).")
    args = parser.parse_args()
    try:
        install(args.mode)
        return 0
    except (OSError, ValueError, SyntaxError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
