#!/usr/bin/env bash
# Standalone TUI uninstaller (Linux).
#
# Removes the `crannia` symlink and the program directory. User data is
# untouched: it lives in ${XDG_DATA_HOME:-$HOME/.local/share}/crannia.

set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd -P) || exit 1
DEST="${CRANNIA_HOME:-$HERE}"
BIN_DIR="${CRANNIA_BIN_DIR:-$HOME/.local/bin}"
LINK="$BIN_DIR/crannia"

# Only remove the command if it points at this installation: the same
# directory can be shared with a GUI install or another app.
if [ -L "$LINK" ]; then
    target=$(readlink -f -- "$LINK" 2>/dev/null || true)
    case "$target" in
        "$DEST"/*)
            rm -f -- "$LINK" && echo "[crannia] removed the crannia command."
            ;;
        *)
            echo "[crannia] left the crannia command alone: it belongs to another install."
            ;;
    esac
elif [ -e "$LINK" ]; then
    echo "[crannia] left $LINK alone: it is not a symlink from this installer."
fi

echo "[crannia] your data stays in ${XDG_DATA_HOME:-$HOME/.local/share}/crannia"

if [ ! -d "$DEST" ]; then
    echo "[crannia] program directory not found: $DEST"
    exit 0
fi

echo "[crannia] removing $DEST"
# Leave the directory before deleting it; the running script itself lives
# there and a live cwd makes some filesystems refuse the rmdir.
cd / || exit 1
rm -rf -- "$DEST"
echo "[crannia] done."
exit 0
