#!/usr/bin/env bash
# Stop hook: ask the agent to append reusable source episodes and queue durable
# assertions silently. Trust is enforced at retrieval, so capture has no numeric
# cap. Human confirmation remains out of band.
set -euo pipefail

MIN_SESSION_AGE_MIN=${KNOWLEDGE_CAPTURE_MIN_AGE_MIN:-20}
REFIRE_HOURS=${KNOWLEDGE_CAPTURE_REFIRE_HOURS:-4}

input=$(cat)
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty')
[ -z "$session_id" ] && exit 0

state_dir="$HOME/.claude/state/knowledge-capture"
mkdir -p "$state_dir"
seen="$state_dir/$session_id.seen"
fired="$state_dir/$session_id.fired"

mtime() { stat -f %m "$1" 2>/dev/null || echo 0; }
now=$(date +%s)

if [ ! -e "$seen" ]; then
  touch "$seen"
  exit 0
fi

if [ -e "$fired" ]; then
  fired_age=$(( now - $(mtime "$fired") ))
  if [ "$fired_age" -lt $(( REFIRE_HOURS * 3600 )) ]; then
    exit 0
  fi
fi

seen_age=$(( now - $(mtime "$seen") ))
if [ "$seen_age" -lt $(( MIN_SESSION_AGE_MIN * 60 )) ]; then
  exit 0
fi

touch "$fired"

reason='Silent knowledge flush (~/AGENTS.md "Capturing facts"). Scan this session for potentially reusable source observations and durable assertions. Append each useful source observation with `knowledge ingest "<observation or quote>" --kind observation --source "session"`; there is no numeric cap. Queue each atomic assertion with `knowledge propose "<fact>" --topic <t> --id <slug> --provenance <user-stated|inferred> --evidence "<quote or where inferred>"`, adding typed subject/predicate/object and repo/path/validator fields when they are directly supported. Hard rules: do NOT use AskUserQuestion, do NOT ask the user to confirm, do NOT run `knowledge add`, and never ingest secrets, credentials, one-off task state, or narrative reasoning. If nothing qualifies, say nothing and stop. Add at most one line of user-facing text about this. Fires at most once per '"$REFIRE_HOURS"'h per session — after this turn, normal stop is allowed.'

jq -n --arg reason "$reason" '{decision: "block", reason: $reason}'
