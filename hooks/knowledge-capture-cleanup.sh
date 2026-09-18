#!/usr/bin/env bash
# SessionStart hook: remove old per-session capture markers.
set -euo pipefail

CLEANUP_DAYS=${KNOWLEDGE_CAPTURE_CLEANUP_DAYS:-30}
state_dir="$HOME/.claude/state/knowledge-capture"
[ -d "$state_dir" ] || exit 0

find "$state_dir" -type f \( -name '*.seen' -o -name '*.fired' \) -mtime "+$CLEANUP_DAYS" -delete 2>/dev/null || true
