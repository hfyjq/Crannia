#!/usr/bin/env bash
# Standalone TUI installer (Linux).
#
# Run this from the extracted package root. It copies the package to
# ${XDG_DATA_HOME:-$HOME/.local/share}/crannia-tui and puts a `crannia`
# command in ~/.local/bin (override with CRANNIA_BIN_DIR).
#
# User data never lives here: conversations, settings and model credentials
# live in ${XDG_DATA_HOME:-$HOME/.local/share}/crannia (shared with the GUI).

set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
DEST="${CRANNIA_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/crannia-tui}"
BIN="${CRANNIA_BIN_DIR:-$HOME/.local/bin}"

if [ ! -x "$HERE/runtime/python/bin/python3" ]; then
    echo "[crannia] this does not look like the extracted package: $HERE" >&2
    echo "[crannia] extract the archive first, then run install.sh from inside it." >&2
    exit 1
fi

if [ "$HERE" = "$DEST" ]; then
    echo "[crannia] already installed at $DEST"
else
    echo "[crannia] installing to $DEST"
    mkdir -p "$DEST" || exit 1
    # `cp -a` keeps the executable bit and the in-package symlinks
    # (python3 -> python3.13); a plain `cp` would dereference them.
    if ! cp -a "$HERE/." "$DEST/"; then
        echo "[crannia] copy failed: $DEST" >&2
        exit 1
    fi
fi

mkdir -p "$BIN" || exit 1
if ! ln -sfn "$DEST/crannia.sh" "$BIN/crannia"; then
    echo "[crannia] could not put the crannia command in $BIN" >&2
    echo "[crannia] run $DEST/crannia.sh directly instead." >&2
    exit 1
fi

echo "[crannia] installed."
echo "[crannia] open a new shell and type: crannia"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "[crannia] note: $BIN is not on PATH yet; add it to your shell profile." ;;
esac
echo "[crannia] uninstall: $DEST/uninstall.sh"
exit 0
