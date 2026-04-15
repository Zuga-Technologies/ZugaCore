#!/usr/bin/env bash
#
# vendor-core.sh — regenerate the `food-truck` branch of a studio.
#
# Produces a Food Truck shaped tree on the studio's `food-truck` branch:
#   1. Removes backend/core/* symlinks (they would dangle with no sibling ZugaCore)
#   2. Vendors ZugaCore backend subsystems into $STUDIO/core/
#   3. Patches backend/main.py sys.path so `core.*` resolves at repo root
#   4. Commits with the vendored ZugaCore SHA in the message
#
# Usage:
#   ../ZugaCore/scripts/vendor-core.sh              (from studio repo root)
#   ZugaCore/scripts/vendor-core.sh ZugaLife        (from parent dir)
#
# Invariants:
#   - main branch is never touched
#   - the transform runs inside a temporary git worktree, so the studio's
#     primary working copy is untouched even on failure
#   - idempotent: running twice produces the same food-truck tree
#
# Scope (backend-only, Option X):
#   Backend subsystems are vendored. Frontend vendoring (Vite alias patch)
#   is a separate follow-up step and NOT handled here.

set -euo pipefail

# --- Resolve paths ----------------------------------------------------------
if [ $# -ge 1 ]; then
    STUDIO_DIR="$(cd "$1" && pwd)"
else
    STUDIO_DIR="$(pwd)"
fi

ZUGACORE_DIR="$(cd "$(dirname "$STUDIO_DIR")/ZugaCore" 2>/dev/null && pwd)" || {
    echo "ERROR: ZugaCore not found as sibling of $(basename "$STUDIO_DIR")" >&2
    echo "Expected at: $(dirname "$STUDIO_DIR")/ZugaCore" >&2
    exit 1
}

STUDIO_NAME="$(basename "$STUDIO_DIR")"
ZUGACORE_SHA="$(cd "$ZUGACORE_DIR" && git rev-parse --short HEAD)"

# Detect the studio's default branch. Some studios are on `main`, others
# still on `master`. Prefer `main` if both exist; fall back to `master`.
cd "$STUDIO_DIR"
if git show-ref --verify --quiet refs/heads/main; then
    BASE_BRANCH="main"
elif git show-ref --verify --quiet refs/heads/master; then
    BASE_BRANCH="master"
else
    echo "ERROR: studio has neither 'main' nor 'master' branch" >&2
    exit 1
fi

# 7 backend subsystems — see food-truck design notes. Frontend excluded.
SUBSYSTEMS=(auth credits database gateway lifecycle plugins theme)

WORKTREE_DIR="$(mktemp -d)/food-truck-$STUDIO_NAME"

echo "=================================================================="
echo "  Vendoring ZugaCore @ $ZUGACORE_SHA"
echo "  Target studio:    $STUDIO_NAME"
echo "  Studio path:      $STUDIO_DIR"
echo "  Base branch:      $BASE_BRANCH"
echo "  Worktree path:    $WORKTREE_DIR"
echo "=================================================================="

# --- Sanity check: studio has a backend/ directory to vendor into ---------
# (Food Court and Food Truck both resolve core.* through backend/core/.
# We don't patch main.py — so we don't need to inspect its sys.path shape.)
if [ ! -d "$STUDIO_DIR/backend" ]; then
    echo "ERROR: $STUDIO_DIR/backend/ not found — nothing to vendor into" >&2
    exit 1
fi

# --- Set up worktree on food-truck branch ----------------------------------
cd "$STUDIO_DIR"

if git show-ref --verify --quiet refs/heads/food-truck; then
    echo ""
    echo "Existing food-truck branch found — regenerating from $BASE_BRANCH"
    git worktree add --force "$WORKTREE_DIR" food-truck >/dev/null
    (
        cd "$WORKTREE_DIR"
        git reset --hard "$BASE_BRANCH" >/dev/null
    )
else
    echo ""
    echo "Creating food-truck branch from $BASE_BRANCH"
    git worktree add --force -b food-truck "$WORKTREE_DIR" "$BASE_BRANCH" >/dev/null
fi

# Guarantee the worktree gets cleaned up even if the script fails partway.
cleanup() {
    local rc=$?
    if [ -d "$WORKTREE_DIR" ]; then
        (cd "$STUDIO_DIR" && git worktree remove --force "$WORKTREE_DIR") >/dev/null 2>&1 || true
    fi
    exit $rc
}
trap cleanup EXIT

# --- 1a. Remove core/ git submodule if present -----------------------------
# ZugaLife (and possibly others) tracks ZugaCore as a git submodule at core/.
# Food Truck mode never uses a repo-root core/, so the submodule gets
# deregistered. Other studios skip this block entirely.
echo ""
echo "[1/4] Deregistering core/ submodule and backend/core/ symlinks..."

cd "$WORKTREE_DIR"
if git ls-files --error-unmatch core >/dev/null 2>&1 && \
   [ "$(git ls-tree HEAD core 2>/dev/null | awk '{print $1}')" = "160000" ]; then
    echo "       core/ is a git submodule — deregistering"
    git submodule deinit --force core >/dev/null 2>&1 || true
    git rm -f --quiet core
    # Strip [submodule "core"] section from .gitmodules if present.
    # If nothing else remains, delete the file from the index entirely.
    if [ -f .gitmodules ]; then
        python - .gitmodules <<'PYEOF'
import re, sys
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    src = f.read()
out = re.sub(r'\[submodule "core"\][^\[]*', '', src).strip()
with open(p, "w", encoding="utf-8", newline="\n") as f:
    f.write(out + ("\n" if out else ""))
PYEOF
        if [ ! -s .gitmodules ] || ! grep -q '^\[submodule' .gitmodules; then
            git rm -f --quiet .gitmodules 2>/dev/null || rm -f .gitmodules
            echo "       removed .gitmodules (no submodules remain)"
        else
            git add .gitmodules
            echo "       cleaned .gitmodules (other submodules preserved)"
        fi
    fi
    # .git/config may still have a core submodule section; clear it.
    git config --local --remove-section submodule.core 2>/dev/null || true
fi

# --- 1b. Remove backend/core/<subsystem> symlinks (committed or not) -------
# The ZUGACORE subsystem slots in backend/core/ may be symlinks pointing
# to ../../../ZugaCore/<sub>. Those are dangling without a sibling
# ZugaCore, so food-truck replaces them with real vendored directories.
if [ -d "$WORKTREE_DIR/backend/core" ]; then
    removed_any=0
    for sub in "${SUBSYSTEMS[@]}"; do
        entry="$WORKTREE_DIR/backend/core/$sub"
        if [ -L "$entry" ]; then
            (cd "$WORKTREE_DIR" && git rm --quiet "backend/core/$sub" 2>/dev/null) || rm -f "$entry"
            echo "       rm symlink: backend/core/$sub"
            removed_any=1
        fi
    done
    if [ $removed_any -eq 0 ]; then
        echo "       (no subsystem symlinks in backend/core/)"
    fi
fi

# --- 2. Vendor ZugaCore subsystems into backend/core/ ----------------------
echo ""
echo "[2/4] Vendoring ZugaCore subsystems into backend/core/..."

# Food Truck vendors into the SAME location Food Court symlinks to
# (backend/core/<subsystem>). This preserves layout parity between modes
# and avoids package-shadowing bugs where a stale repo-root core/ would
# block Python from reaching the vendored tree.
#
# Local extensions (e.g., backend/core/ai/) are preserved because the
# vendor only overwrites the 7 known subsystem names.

BACKEND_CORE="$WORKTREE_DIR/backend/core"
mkdir -p "$BACKEND_CORE"
# Ensure backend/core is a real package so studio-local modules
# (backend/core/ai/, etc.) remain importable.
if [ ! -f "$BACKEND_CORE/__init__.py" ]; then
    : > "$BACKEND_CORE/__init__.py"
fi

for sub in "${SUBSYSTEMS[@]}"; do
    src="$ZUGACORE_DIR/$sub"
    dst="$BACKEND_CORE/$sub"
    if [ ! -d "$src" ]; then
        echo "       WARN: $sub not found in ZugaCore, skipping" >&2
        continue
    fi
    # Clean-slate each subsystem: wipes stale contents, skips any
    # local additions that aren't in the ZugaCore subsystem set.
    rm -rf "$dst"
    cp -r "$src" "$dst"
    find "$dst" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find "$dst" -type f -name '*.pyc' -delete 2>/dev/null || true
    find "$dst" -type f -name '.DS_Store' -delete 2>/dev/null || true
    # Force-add past .gitignore — studios typically ignore these paths in
    # Food Court mode so accidentally-committed symlinks don't leak. On
    # food-truck they are real files and MUST be tracked.
    (cd "$WORKTREE_DIR" && git add -f "backend/core/$sub") >/dev/null
    echo "       vendored: backend/core/$sub/"
done

# --- 2b. Strip dev-only gitignore lines that reference the vendored paths --
# These rules exist in master to hide setup-symlinks.sh symlinks from commits.
# On food-truck the same paths hold real files and must NOT be ignored.
if [ -f "$WORKTREE_DIR/.gitignore" ]; then
    python - "$WORKTREE_DIR/.gitignore" "${SUBSYSTEMS[@]}" <<'PYEOF'
import sys
path = sys.argv[1]
subs = set(sys.argv[2:])
with open(path, encoding="utf-8") as f:
    lines = f.readlines()
keep = []
removed = []
for line in lines:
    stripped = line.strip().lstrip("/")
    # Match "backend/core/<sub>" with or without leading slash or trailing slash.
    if stripped.startswith("backend/core/"):
        name = stripped[len("backend/core/"):].rstrip("/")
        if name in subs:
            removed.append(stripped)
            continue
    keep.append(line)
if removed:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(keep)
    print(f"       stripped gitignore rules: {', '.join(removed)}")
else:
    print("       (.gitignore has no dev-only backend/core rules)")
PYEOF
    (cd "$WORKTREE_DIR" && git add .gitignore) >/dev/null 2>&1 || true
fi

# --- 3. Remove any stray repo-root core/ (from deprecated vendor layout) ---
echo ""
echo "[3/4] Ensuring no repo-root core/ package can shadow backend/core/..."
if [ -d "$WORKTREE_DIR/core" ] || [ -f "$WORKTREE_DIR/core" ]; then
    rm -rf "$WORKTREE_DIR/core"
    (cd "$WORKTREE_DIR" && git rm -rf --quiet --ignore-unmatch core) >/dev/null 2>&1 || true
    echo "       removed: core/ (repo root)"
else
    echo "       (no repo-root core/ — nothing to remove)"
fi

# --- 4. Commit the result --------------------------------------------------
echo ""
echo "[4/4] Committing food-truck tree..."
cd "$WORKTREE_DIR"
git add -A

if git diff --cached --quiet; then
    echo "       No changes to commit — food-truck branch already matches current ZugaCore."
else
    git commit --quiet -m "food-truck: vendor ZugaCore @ $ZUGACORE_SHA

Regenerated by vendor-core.sh.

Subsystems vendored:
  ${SUBSYSTEMS[*]}

Transform applied:
  - removed backend/core/* symlinks
  - copied ZugaCore subsystems into core/ at repo root
  - patched backend/main.py sys.path (parent -> parent.parent)

Main branch not touched. Backend-only (Option X); frontend vendoring is
a separate follow-up pass."
    echo "       committed to food-truck branch"
fi

NEW_HEAD="$(git rev-parse --short HEAD)"

echo ""
echo "=================================================================="
echo "  Done. food-truck is ready."
echo "  Branch HEAD:      $NEW_HEAD"
echo "  Vendored from:    ZugaCore @ $ZUGACORE_SHA"
echo ""
echo "  Inspect:          git log food-truck --oneline -5"
echo "  Check out:        git checkout food-truck"
echo "  Push (when OK):   git push origin food-truck"
echo "=================================================================="
