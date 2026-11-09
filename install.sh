#!/bin/sh
# Install the CLI for the current user without elevated privileges.
set -eu

usage() {
    printf 'Usage: %s [--help]\n' "${0##*/}"
    printf 'Install vogsync.py into ~/.local/bin (or $HOME/.local/bin).\n'
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
mkdir -p "$destination_dir"
if [ -e "$destination" ] || [ -L "$destination" ]; then
    printf 'Error: %s already exists; no file was overwritten.\n' "$destination" >&2
    exit 1
fi
if ! (set -C; umask 022; cat "$source_file" > "$destination") 2>/dev/null; then
    printf 'Error: could not create %s.\n' "$destination" >&2
    exit 1
fi
chmod 755 "$destination"
printf 'Installed: %s\n' "$destination"
case ":${PATH:-}:" in
    *":$destination_dir:"*) ;;
    *)
        printf 'Add this directory to your PATH, for example in ~/.profile:\n'
        printf '  export PATH="$HOME/.local/bin:$PATH"\n'
        ;;
esac
