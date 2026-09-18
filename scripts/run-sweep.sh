#!/usr/bin/env bash
set -euo pipefail

# Run from cron/launchd or another external scheduler. Store state remains
# external because the CLI resolves it before writing validation/run records.
budget="${KNOWLEDGE_SWEEP_BUDGET:-100}"
run_id="${KNOWLEDGE_SWEEP_RUN_ID:-scheduled-$(date -u +%Y%m%dT%H%M%SZ)}"
exec knowledge sweep --limit "$budget" --run-id "$run_id" --json
