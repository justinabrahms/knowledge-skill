#!/usr/bin/env bash
# UserPromptSubmit hook: retrieve confirmed assertions relevant to the prompt.
set -euo pipefail

[ "${KNOWLEDGE_RECALL:-1}" = "0" ] && exit 0

KNOWLEDGE_BIN=${KNOWLEDGE_BIN:-$HOME/bin/knowledge}
[ -x "$KNOWLEDGE_BIN" ] || exit 0

input=$(cat)
prompt=$(printf '%s' "$input" | jq -r '.prompt // empty')

# Attribute the lookup. The harness hands hooks the session id on stdin, which
# is authoritative; the env var is the fallback for harnesses that only export
# it. Without this, hook-driven recalls — the overwhelming majority — show up
# unattributed in `knowledge retrieval-health --by agent`.
session=$(printf '%s' "$input" | jq -r '.session_id // empty')
export KNOWLEDGE_AGENT="${KNOWLEDGE_AGENT:-claude}"
export KNOWLEDGE_SESSION_ID="${KNOWLEDGE_SESSION_ID:-${session:-${CLAUDE_CODE_SESSION_ID:-}}}"

# Slash commands and short acknowledgements do not have useful retrieval signal.
case "$prompt" in
  /*) exit 0 ;;
esac
[ "${#prompt}" -lt 15 ] && exit 0

# Never let retrieval block a prompt. macOS calls GNU coreutils `gtimeout`.
if command -v gtimeout >/dev/null 2>&1; then
  out=$(gtimeout 10s "$KNOWLEDGE_BIN" recall "$prompt" --limit 3 --quiet 2>/dev/null) || exit 0
elif command -v timeout >/dev/null 2>&1; then
  out=$(timeout 10s "$KNOWLEDGE_BIN" recall "$prompt" --limit 3 --quiet 2>/dev/null) || exit 0
else
  out=$("$KNOWLEDGE_BIN" recall "$prompt" --limit 3 --quiet 2>/dev/null) || exit 0
fi
[ -z "$out" ] && exit 0

printf '%s\n' "$out"
