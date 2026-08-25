#!/usr/bin/env bash
# Pre-publish gate: fail if anything private is about to be published.
#
# This tool was extracted from a working private store, so the failure mode is
# not "someone types a secret" — it is an illustrative example, a test fixture,
# or a code comment quoting the real corpus it was built against. Those read as
# generic until you know the organisation, which is why a human skim is not a
# control. Run this before every push.
#
#   scripts/check-no-internal-data.sh
#
# The patterns are NOT stored here. A deny-list naming your employer, its vendor
# stack, its ticket prefixes and its cluster naming is itself a disclosure — the
# gate would become the most revealing file in the repo. Keep yours outside the
# tree and point the gate at it:
#
#   cp scripts/scrub-patterns.example ~/.config/knowledge/scrub-patterns.txt
#   $EDITOR ~/.config/knowledge/scrub-patterns.txt
#
# Override the location with KNOWLEDGE_SCRUB_PATTERNS.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PATTERN_FILE="${KNOWLEDGE_SCRUB_PATTERNS:-$HOME/.config/knowledge/scrub-patterns.txt}"
status=0

# Structural checks run with or without a pattern file: these filenames are
# generated store content regardless of whose store it is.
found=$(find . -name 'plan.json' -o -name 'applied-*.json' -o -name 'review-cards*.py' \
        -o -name '.review-cards.py' -o -name '.review-plan.json' \
        -o -name '*.bak' -o -name 'knowledge.bak.*' 2>/dev/null | grep -v '^./.git/' || true)
if [ -n "$found" ]; then
  echo "FAIL  store content, not tool code:"
  printf '%s\n' "$found" | sed 's/^/      /'
  status=1
fi

if [ ! -f "$PATTERN_FILE" ]; then
  echo "WARN  no pattern file at $PATTERN_FILE — content scan skipped."
  echo "      cp scripts/scrub-patterns.example \"$PATTERN_FILE\" and edit it."
  exit "$status"
fi

# One ERE per line. Blank lines and #-comments ignored.
while IFS= read -r pat; do
  case "$pat" in ''|'#'*) continue ;; esac
  hits=$(grep -rInE "$pat" . \
      --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.pytest_cache \
      --exclude-dir=node_modules \
      --exclude="$(basename "$0")" 2>/dev/null)
  if [ -n "$hits" ]; then
    echo "FAIL  /$pat/"
    printf '%s\n' "$hits" | sed 's/^/      /'
    status=1
  fi
done < "$PATTERN_FILE"

[ "$status" -eq 0 ] && echo "clean — no internal data found"
exit "$status"
