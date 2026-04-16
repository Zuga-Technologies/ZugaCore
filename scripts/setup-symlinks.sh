#!/usr/bin/env bash
#
# Creates ZugaCore backend symlinks for a studio or ZugaApp repo.
#
# Usage:
#   ../ZugaCore/scripts/setup-symlinks.sh          (from repo root)
#   ZugaCore/scripts/setup-symlinks.sh ZugaLife     (from parent dir)
#
# Assumes sibling layout: Projects/ZugaCore, Projects/ZugaLife, Projects/ZugaApp, etc.

set -euo pipefail

# Determine the target repo
if [ $# -ge 1 ]; then
    REPO_DIR="$(cd "$1" && pwd)"
else
    REPO_DIR="$(pwd)"
fi

# Find ZugaCore relative to the repo
ZUGACORE_DIR="$(cd "$(dirname "$REPO_DIR")/ZugaCore" 2>/dev/null && pwd)" || {
    echo "ERROR: ZugaCore not found as sibling of $(basename "$REPO_DIR")"
    echo "Expected at: $(dirname "$REPO_DIR")/ZugaCore"
    exit 1
}

CORE_TARGET="$ZUGACORE_DIR"
BACKEND_CORE="$REPO_DIR/backend/core"

echo "Setting up ZugaCore symlinks for: $(basename "$REPO_DIR")"
echo "  ZugaCore: $ZUGACORE_DIR"
echo ""

# Detect OS and create symlinks accordingly
create_link() {
    local target="$1"
    local link="$2"
    local name="$(basename "$link")"

    # Safe remove. Symlinks use `rm -f` (never follows the link); real
    # files/dirs use `rm -rf`. The old unified `rm -rf "$link"` could
    # descend into the symlink's target on MSYS Windows when the stored
    # target string ended with '/', silently nuking the canonical ZugaCore
    # directory. Splitting the two cases avoids that class of accident.
    if [ -L "$link" ]; then
        rm -f "$link"
    elif [ -e "$link" ]; then
        rm -rf "$link"
    fi

    # Compute relative target from the link's parent directory so the
    # stored symlink is portable across machines (Buga desktop, Mac Mini,
    # Linux CI, fresh clones). Matches the `credits` symlink convention
    # already tracked in ZugaApp. Python's os.path.relpath works
    # identically on all three OSes — same dep already used by
    # vendor-core.sh for .gitmodules / .gitignore rewrites.
    local rel_target
    rel_target="$(python -c "import os, sys; print(os.path.relpath(sys.argv[1], sys.argv[2]).replace(os.sep, '/'))" "$target" "$(dirname "$link")")"

    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
        # Windows: mklink /d accepts relative paths; backslashes required.
        # NOTE: mklink.exe still needs admin or Developer Mode to create
        # new symlinks. If this fails, prefer the git-tracked symlink
        # pattern (see ZugaApp's credits/lifecycle): git update-index
        # --add --cacheinfo 120000,<hash>,<path> then git checkout.
        local win_link
        if command -v cygpath >/dev/null 2>&1; then
            win_link="$(cygpath -w "$link")"
        else
            win_link="$(echo "$link" | sed 's|^/\([a-zA-Z]\)/|\U\1:/|; s|/|\\|g')"
        fi
        local win_rel_target="${rel_target//\//\\}"
        cmd //c "mklink /d \"$win_link\" \"$win_rel_target\"" > /dev/null
    else
        # macOS / Linux: ln -s accepts relative paths directly.
        ln -s "$rel_target" "$link"
    fi

    echo "  $name -> $rel_target"
}

# Ensure backend/core directory exists
mkdir -p "$BACKEND_CORE"

# Create the 7 backend symlinks. Must match SUBSYSTEMS in vendor-core.sh —
# any subsystem present in one script and absent in the other leaves studios
# running stale forks in dev mode (the Food Court / Food Truck drift trap).
echo "Backend symlinks:"
create_link "$CORE_TARGET/auth"      "$BACKEND_CORE/auth"
create_link "$CORE_TARGET/credits"   "$BACKEND_CORE/credits"
create_link "$CORE_TARGET/database"  "$BACKEND_CORE/database"
create_link "$CORE_TARGET/gateway"   "$BACKEND_CORE/gateway"
create_link "$CORE_TARGET/lifecycle" "$BACKEND_CORE/lifecycle"
create_link "$CORE_TARGET/plugins"   "$BACKEND_CORE/plugins"
create_link "$CORE_TARGET/theme"     "$BACKEND_CORE/theme"

echo ""
echo "Done! Backend can now import from:"
echo "  core.auth, core.credits, core.database, core.gateway,"
echo "  core.lifecycle, core.plugins, core.theme"
echo "(Frontend uses Vite resolve.alias — no symlink needed)"
