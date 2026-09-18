---
name: knowledge
description: Query and update an external, repo-aware, temporal knowledge graph through the `knowledge` CLI. Use for durable organizational facts, ownership, topology, conventions, decisions, or user preferences; for capturing source episodes; and when stored assertions need validation or refutation.
---

# knowledge — external temporal knowledge graph

The store and generated SQLite graph live outside every repository they
describe. Repository awareness comes from canonical `owner/repo`, commit, path,
and typed-entity references. Never create knowledge files or indexes inside an
application repository.

CLI: `~/bin/knowledge` (a symlink to this repository's `bin/knowledge`). Use the
absolute path in hooks. Run `knowledge config` before writing when store
selection is uncertain.

## Three layers

| Layer | Command | Trust |
|---|---|---|
| Episode | `knowledge ingest` | Untrusted raw evidence |
| Candidate assertion | `knowledge propose` | Unvetted lead |
| Confirmed assertion | `knowledge confirm` | Citable subject to status and time |

The boundary is enforced at retrieval. Episodes and candidates never enter
unattended recall, so ingestion does not need a numeric rate limit.

## Capture without throwing observations away

Use `ingest` silently for potentially reusable source material. It is
append-only and deliberately has no dedupe gate:

```bash
knowledge ingest "<observation or quote>" \
  --kind observation --source "<source>" \
  [--repo owner/name] [--revision <sha>] [--path <repo-relative-path>]
```

Do not ingest secrets, credentials, one-off task status, or narrative reasoning.
Long-form investigation belongs in session notes.

Use `propose` for an atomic assertion worth retrieving later. Link existing
episodes with repeatable `--episode`; without one, `propose` creates an episode
from its evidence automatically.

```bash
knowledge propose "<one sentence>" --topic <t> \
  --provenance user-stated|inferred --evidence "<quote or source>" \
  [--subject service:checkout --predicate owned_by --object team:widgets] \
  [--valid-from YYYY-MM-DD] [--valid-to YYYY-MM-DD]
```

For `user-stated`, quote the person. For `inferred`, give evidence that can be
checked without repeating the investigation. Capture liberally; consolidation,
trust-aware retrieval, and sweeping control quality after ingestion.

`add`, `confirm`, and `reject` remain human operations. Never cite an episode or
candidate as established, and never derive a new citable assertion solely from
an unconfirmed candidate.

## Read before asserting

Before asserting ownership, topology, conventions, or “how do we do X”, check:

- `knowledge recall "<prompt>"` — gated hook lookup; automatically scopes to the
  current repository when run in a checkout.
- `knowledge search "<query>"` — explicit ranking; supports `--repo`,
  `--all-repos`, and `--as-of`.
- `knowledge topics` / `knowledge list --topic <t>` / `knowledge get <id>`.
- `knowledge graph-neighbors <entity> [--depth 1..3]` for typed relations.

Repository-specific assertions for other repositories are excluded by default;
org/global assertions remain eligible. Commit-validity fields are evaluated
against the current checkout when available.

Treat metadata as load-bearing:

- `confidence: low` is a hint, not authority.
- `epistemic_status: unknown|contested` must be surfaced with the assertion.
- `refuted`, `superseded`, or `invalidated_at` assertions are not retrievable.
- `valid_from`/`valid_to` are world time; `recorded_at` and validation events are
  system time. `--as-of` queries world time.
- A stale assertion must be flagged or swept before relying on it.

## Validation and refutation

Typed assertions can carry deterministic validator specifications. Current
built-ins are `git-path` for `contains_path`/`has_path` relations and
`git-file-contains` for literal source evidence.

```bash
knowledge propose "The repo contains deploy.yaml." --topic deploy \
  --repo acme/widgets \
  --subject repo:acme/widgets --predicate contains_path --object path:deploy.yaml \
  --repo-path deploy.yaml --validator git-path

knowledge sweep                         # every validator-backed assertion
knowledge sweep <id>                    # one assertion
knowledge sweep --repo acme/widgets --stale-only
```

Sweeps append validation observations and update epistemic state:

- authoritative agreement → `supported` and refreshed verification time;
- authoritative contradiction → `refuted`;
- unavailable repository/source or insufficient structure → `unknown`, never
  refuted.

If you independently observe a contradiction where no validator applies, run
`knowledge invalidate <id> --reason "<what contradicted it>"`, then ingest the
evidence and propose the replacement.

## Graph projection

`knowledge graph-rebuild` materializes entities, typed assertion edges,
episodes, and validation history into an external SQLite cache. It is disposable
and rebuilt from the store; never treat it as the source of truth.

For an existing legacy store, `knowledge migrate` is an idempotent structural
backfill. It creates labelled untrusted source episodes and safe metadata
defaults, but never fabricates typed graph relations from prose.

`knowledge usage` reports whether retrieval is effective. `knowledge dupes` and
`knowledge tune` use review outcomes to improve consolidation thresholds.
