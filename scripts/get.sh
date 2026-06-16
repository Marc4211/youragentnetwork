#!/usr/bin/env bash
#
# Agent Network bootstrap. Run this ON THE BOX where your OpenClaw already runs:
#
#   curl -fsSL https://raw.githubusercontent.com/Marc4211/youragentnetwork/main/scripts/get.sh | bash
#
# It checks prerequisites, fetches the package, and launches the browser setup
# wizard. (Requires the repo to be public, or an existing checkout on the box.)

set -euo pipefail

REPO_URL="${ACN_REPO_URL:-https://github.com/Marc4211/youragentnetwork.git}"
DEST="${ACN_DIR:-$HOME/youragentnetwork}"

echo "==> Agent Network setup"

command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is required. Install Docker, then re-run."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: Docker Compose v2 is required."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required."; exit 1; }

# Use an existing checkout if we're already inside one; else clone.
if [ -f "scripts/wizard.py" ]; then
  DEST="$(pwd)"
elif [ -f "$DEST/scripts/wizard.py" ]; then
  echo "    using existing checkout at $DEST"
else
  command -v git >/dev/null 2>&1 || { echo "ERROR: git is required to fetch the package."; exit 1; }
  echo "    fetching into $DEST ..."
  git clone --depth 1 "$REPO_URL" "$DEST"
fi

cd "$DEST"
echo "    launching the setup wizard ..."
exec python3 scripts/wizard.py
