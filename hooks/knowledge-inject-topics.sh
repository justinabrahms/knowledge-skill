#!/usr/bin/env bash
# SessionStart hook: inject a compact directory of confirmed knowledge topics.
set -euo pipefail

WARN_FACTS=${KNOWLEDGE_INDEX_WARN_FACTS:-250}
KNOWLEDGE_BIN=${KNOWLEDGE_BIN:-$HOME/bin/knowledge}

[ -x "$KNOWLEDGE_BIN" ] || exit 0
topics=$("$KNOWLEDGE_BIN" topics --plain 2>/dev/null || true)
[ -z "$topics" ] && exit 0
topic_count=$(printf '%s\n' "$topics" | wc -l | tr -d ' ')
fact_count=$(printf '%s\n' "$topics" | awk -F'[()]' '{s+=$2} END {print s+0}')

{
  echo "# Knowledge topics (${topic_count} topics, ${fact_count} confirmed assertions)"
  echo
  echo "Each line is \`<topic> (<fact-count>)\`. To go deeper:"
  echo "  - \`knowledge list --topic <topic>\` — IDs and metadata for one topic"
  echo "  - \`knowledge get <id>\` — body and frontmatter for one assertion"
  echo "  - \`knowledge search \"<query>\"\` — repo- and date-aware retrieval"
  echo "  - \`knowledge graph-neighbors <entity>\` — typed relations"
  echo
  if [ "$fact_count" -gt "$WARN_FACTS" ]; then
    echo "⚠️  ${fact_count} confirmed assertions (>${WARN_FACTS}). Consider \`knowledge list --stale\`, \`knowledge list --invalid\`, or topic consolidation."
    echo
  fi
  printf '%s\n' "$topics"
}
