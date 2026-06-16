#!/usr/bin/env bash
#
# scripts/tunnel.sh - reach a loopback-profile Agent Network host from your
# own machine. Opens an SSH tunnel so http://localhost:3000 (chat) and
# http://localhost:8000/join (join page) on YOUR machine map to the server.
#
# Run this on your laptop, not the server. Leave it running; Ctrl-C to stop.
#
# Usage:
#   bash scripts/tunnel.sh user@host [path-to-ssh-key]
#
# Example:
#   bash scripts/tunnel.sh root@203.0.113.5 ~/.ssh/agentnetwork_portable

set -euo pipefail
TARGET="${1:?usage: tunnel.sh user@host [ssh-key-path]}"
KEY="${2:-}"
KEYOPT=()
[ -n "$KEY" ] && KEYOPT=(-i "$KEY")

echo "Tunneling to $TARGET:"
echo "  chat -> http://localhost:3000"
echo "  join -> http://localhost:8000/join"
echo "Leave this running; Ctrl-C to stop."
exec ssh "${KEYOPT[@]}" -N \
  -L 3000:127.0.0.1:3000 \
  -L 8000:127.0.0.1:8000 \
  "$TARGET"
