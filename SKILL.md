---
name: knowledge
description: Query and update an agent-maintained atomic-fact store via the `knowledge` CLI. Use when answering or making assumptions about infrastructure, team or service ownership, deployment topology, tooling decisions, team practices, or user preferences — anything where a stored canonical fact might exist. Also use to queue new facts and to invalidate facts when you observe a contradiction.
---

# knowledge — atomic-fact store

One fact per markdown file, schema-validated frontmatter, tf-idf duplicate
detection. Nothing about a particular organisation is compiled in: the store
location, scope vocabulary, similarity thresholds, and identifier classes come
from a `.knowledge.yml` found by walking up from the working directory.

CLI: `bin/knowledge` in this skill directory. Invoke it by absolute path —
do not assume it is on `PATH`.

    "$CLAUDE_SKILL_DIR/bin/knowledge" topics

If no store is configured, `knowledge init` creates one. Run `knowledge --help`
for the full command list; the paragraphs below are the parts that are not
obvious from the help text.

## Read before you assert

The store is worthless if it is not consulted. Before asserting ownership,
topology, conventions, or "how do we do X here", check:

- `knowledge recall "<the prompt>"` — the gated lookup a prompt hook runs. If a
  `# Recalled facts` block is already in your context, those facts are loaded;
  do not re-fetch them. It is not exhaustive, so a thin block is not evidence
  that nothing is stored — fall through to the rest of this list.
- `knowledge search "<query>"` — ungated ranking, for when you are the one asking
- `knowledge topics` — what subject areas exist, with fact counts
- `knowledge list --topic <t>` — ids, scope, confidence, staleness in one topic
- `knowledge get <id>` — full body and frontmatter
- `knowledge index --topic <t>` — one line per fact; `--all` grows with the store

Frontmatter is load-bearing. `confidence: low` is a hint, not authority.
A `last_verified_at` past `stale_days`, or a `valid_until` in the past, means
surface the fact **and** flag the staleness rather than relying on it silently.
`invalidated_at` set means do not use it at all — the file survives only for
audit. Provenance predicts reliability better than confidence, because
confidence is self-assigned by whatever wrote the fact.

## Write only to the queue

`knowledge propose` is the only write you make. `add`, `confirm`, and `reject`
belong to the human.

    knowledge propose "<one sentence>" --topic <t> \
      --provenance user-stated|inferred --evidence "<quote or path:line>"

Capture liberally — rejecting a candidate at review is cheaper than re-deriving
a lost fact three times. Duplicates get filtered on the review end by
`knowledge dupes`, so do not hold back out of tidiness. Propose silently: no
questions, no "should I save this?".

Candidates land as `.yml` in `pending/` specifically so a markdown indexer never
picks them up. That is the safety property of the whole design — unreviewed
output can accumulate for months without contaminating what agents read back as
fact. Never propose a fact derived from an unconfirmed candidate; that launders
inference into the store one hop at a time.

`--evidence` is what makes review a one-second decision, and it is stored on the
fact rather than discarded at promotion — so it stays the thing that lets a
future reader re-check the claim without redoing the work. For `user-stated`,
quote the person. For `inferred`, give the file and line. An inferred candidate a
reviewer cannot verify from its evidence line is worse than no candidate.

## When you find the store is wrong

    knowledge invalidate <id> --reason "<what you actually observed>"

Then propose the replacement. Say one line about it. Do not silently work around
a stale fact — the next session will hit the same wall.

## Reviewing (human-driven)

`knowledge dupes` groups near-duplicate candidates so a reviewer decides once
per cluster instead of once per candidate. Treat duplicate clusters as a
correctness signal: when several candidates restate one claim while naming
*different* magnitudes for the same quantity, most of them are wrong, and the
merged fact should usually state no number rather than pick one.

Nothing is destroyed by review hygiene: `prune-pending` moves aged-out
candidates and old rejections into `pending/archived/` instead of deleting them,
and archived entries are out of the duplicate corpus, so a claim that matters
again can simply be proposed again.

`knowledge usage` answers whether any of this is working — per-fact read counts,
recall hit rate, and the facts nothing has ever surfaced.

Reject with a reason that names the surviving fact. That blocks re-proposal, and
it is the labelled data `knowledge tune` uses to derive thresholds from your own
review history rather than from someone else's corpus.
