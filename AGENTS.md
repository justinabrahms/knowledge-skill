# knowledge-skill

## Architecture Context

- Architecture Decision Records are in `docs/adrs/`.
- Specifications are in `docs/openspec/specs/`.
- The knowledge store, episode log, validation log, and SQLite projection MUST
  remain outside repositories described by assertions.
- qmd collections for this repository are `knowledge-adrs`, `knowledge-specs`,
  and `knowledge-code`. Run `qmd update` after changing architecture artifacts.

## Verification

Run `./run-tests.sh`, `./scripts/check-no-internal-data.sh`, and
`git diff --check` before committing. Validate the skill with the installed
Codex skill validator.
