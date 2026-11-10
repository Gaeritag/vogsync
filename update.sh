#!/bin/sh
# Update the installed CLI from this checkout without elevated privileges.
set -eu

usage() {
    printf 'Usage: %s [--help]\n' "${0##*/}"
    printf 'Update ~/.local/bin/vogsync from vogsync.py beside this script.\n'
    printf 'Run git pull --ff-only first to obtain the latest source.\n'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'Error: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "${HOME:-}" ]; then
    printf 'Error: HOME is not set.\n' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf 'Error: python3 is required.\n' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    printf 'Error: Python 3.9 or newer is required.\n' >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    printf 'Error: Git is required.\n' >&2
    exit 1
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_file=$script_dir/vogsync.py
destination_dir=$HOME/.local/bin
destination=$destination_dir/vogsync
if [ ! -f "$source_file" ]; then
    printf 'Error: cannot find %s.\n' "$source_file" >&2
    exit 1
fi
if [ -L "$destination" ] || { [ -e "$destination" ] && [ ! -f "$destination" ]; }; then
    printf 'Error: %s must be a regular file, not a symlink or directory.\n' "$destination" >&2
    exit 1
fi
if [ ! -f "$destination" ]; then
    printf 'Error: %s is not installed; run ./install.sh first.\n' "$destination" >&2
    exit 1
fi

# Stage beside the installed file so replacement is atomic on the same filesystem.
temporary=$(mktemp "$destination_dir/.vogsync-update.XXXXXX")
trap 'rm -f -- "$temporary"' 0
trap 'exit 1' HUP INT TERM
cat "$source_file" > "$temporary"
python3 - "$temporary" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
compile(path.read_bytes(), "vogsync.py", "exec")
PY
chmod 755 "$temporary"
python3 - "$temporary" "$destination" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
printf 'Updated: %s\n' "$destination"
