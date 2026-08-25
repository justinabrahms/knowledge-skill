# knowledge

An atomic-fact store for agent memory. One fact per markdown file, schema-checked
frontmatter, and duplicate detection that survives paraphrase.

*Every measured number below — similarity scores, threshold values, queue sizes —
comes from one private store built by one person. They are offered as evidence
that the design decisions were made against real data, not as defaults that will
hold for yours. `knowledge tune` derives your own.*

    knowledge init                       # writes .knowledge.yml + the store + the agent protocol
    knowledge propose "..." --topic t --provenance inferred --evidence "src/x.py:12"
    knowledge dupes                      # group near-duplicate candidates for review
    knowledge confirm <id> | reject <id> --reason "duplicate of <survivor>"
    knowledge recall "$PROMPT" --quiet   # gated retrieval, for a prompt hook
    knowledge usage --unread             # which stored facts nothing ever reads
    knowledge tune                       # derive thresholds from your own review history

## Why two tiers

Agents write to a queue (`pending/*.yml`); humans promote into the store
(`*.md`). The queue is YAML in a subdirectory on purpose: a markdown indexer
never sees it, so unreviewed model output can accumulate indefinitely without
contaminating what agents read back as fact.

That property earned its keep. In the store this tool was extracted from, 198
unvetted candidates piled up over six weeks without a single one being reviewed
— and retrieval was never affected, because nothing could reach them.

## Retrieval

A store nothing reads back is a diary. `search` is for a human who already
suspects the answer is in here; `recall` is for a prompt hook that runs on every
message with nobody watching, so it is gated and returns nothing rather than
something weak.

Ranking is the geometric mean of tf-idf cosine and *query coverage* — the share
of the query's idf mass the fact accounts for. Cosine alone over-rewards a short
fact that collides with one rare token, which is how "who owns the checkout
service" matched a fact about `git checkout`.

Eligibility, though, is a token gate rather than a score gate, and that
distinction was measured rather than assumed. On real prompts a conversational
aside ("did you do the config file thing too?") scored *higher* than a genuine
question about the deploy pipeline, so no floor separates them; matched idf mass
and token rarity fail the same way. In a store of a few hundred facts about one
organisation, "config" and the name of your deploy tool have identical document
frequency, so idf cannot tell domain vocabulary from filler. What separates them
is only ever *which* tokens matched. `recall_generic` lists the filler: those
tokens still count toward the score, but a fact matched only on them is not
eligible. Extend that list when recall fires on noise — it is the maintenance
surface, not the floor.

`recall` prints confirmed facts only. Matching candidates are named so you can
go read them, never pasted inline. Stale and expired facts are surfaced *with*
their marker rather than filtered out, so retrieval cannot quietly launder an
old fact into a current answer.

## Knowing whether any of it is used

Reads append to `.usage.jsonl` in the store. `knowledge usage` reports per-fact
read counts, the recall hit rate, and how many facts have never been surfaced at
all. Without it, curation can only prune by age, which says nothing about
whether a fact ever changed an answer — and `usage --misses` lists the queries
that matched nothing, which is the only honest input for retuning the floor.
`KNOWLEDGE_USAGE_LOG=0` disables it.

## Duplicate detection

Similarity is tf-idf cosine, not token overlap. Token overlap does not survive
paraphrase: in the corpus this was built against, genuine duplicate pairs scored
between 0.26 and 0.67 Jaccard, so a 0.75 gate caught nothing for three months.

Three thresholds, deliberately ordered:

| setting | role |
|---|---|
| `duplicate_cosine` | `propose` refuses. Strictest — refusing is destructive. |
| `related_cosine` | queue it anyway, but record neighbours in `near:` |
| `cluster_cosine` | grouping for `dupes`. Loosest — review can split a group with one keystroke, but a missed duplicate becomes a second confirmed fact. |

Do not copy these numbers. `knowledge tune` derives them from your rejection log:
every candidate rejected with a reason naming the fact it duplicated is a
labelled positive pair, so you can measure what a threshold would have caught
instead of guessing. Since grouping is single-linkage, the statistic that matters
is each member's *best* edge to a clustermate, not the average pair.

## The entity guard

Similarity metrics weight words by how common they are, so a shared sentence
template drowns out the rare tokens carrying the meaning:

> "The Payments team files its work in the Jira **PAY** project."
> "The Search team files its work in the Jira **SRCH** project."

These score 0.42 on tf-idf and rank each other second under sentence embeddings.
They are unrelated facts. So configured identifier classes get a veto that runs
before similarity is consulted.

The rule is narrow on purpose: a merge is blocked only when each side names
**exactly one** identifier of a class and they differ. A fact naming one
identifier is *about* it; a fact listing several is illustrating with them, and
those must stay mergeable. Measured against 150 pairs from 11 hand-verified
duplicate clusters, the guard wrongly vetoed zero of them.

Leave `entities: []` to disable it.

## Nothing is deleted

`prune-pending` moves aged-out candidates and old rejections to
`pending/archived/` rather than unlinking them. A candidate aging out means
review didn't keep up, which is not a finding that the claim was wrong, and
destroying text nobody ever judged settles nothing. Archived entries stay *out*
of the duplicate corpus on purpose: if the same fact matters again, an agent
re-proposing it is the signal that it does.

`evidence` carries from `propose` through `confirm` into the stored fact. It used
to be dropped at promotion, which left `source: session-<date>` as a fact's only
pointer — unverifiable without redoing the original research.

## Review UI

`review/` is a local one-card-one-decision client for the queue: `build_plan.py`
groups it into a deck, `review.py` serves it on localhost, and every write shells
out to this CLI rather than touching the store directly. `test_keys.mjs` drives
the real page in headless Chrome over CDP, because the bug it guards — edit mode
swallowing bare-letter shortcuts — is invisible to anything that reasons about
the handler instead of pressing keys.

Neither of the two files it reads and writes lives in this tree. The generated
deck (`<store>/.review-plan.json`) and any hand-authored merge cards
(`<store>/.review-cards.py`) both describe one store's actual contents, so they
live in that store, beside `.knowledge.yml` and `.usage.jsonl`. Per-store rather
than global is the load-bearing part: a global path collides the moment there is
a second store, and merge decisions written for one corpus are meaningless
against another. That they also sit outside any repository — where no `git add
-f` can reach them — falls out of the same rule.

The only genuinely global file is `~/.config/knowledge/config.yml`, which names
the default store and nothing else.

## Before publishing

    cp scripts/scrub-patterns.example ~/.config/knowledge/scrub-patterns.txt
    $EDITOR ~/.config/knowledge/scrub-patterns.txt
    scripts/check-no-internal-data.sh

This was extracted from a working private store, so the leak risk is not someone
typing a secret — it is an illustrative example, a test fixture, or a code
comment quoting the real corpus. Those read as generic unless you know the
organisation, which is why a human skim is not a control. The gate has already
caught a generated deck carrying a container-registry name, GitHub App secret
names, an employee address and incident IDs.

The patterns live outside the repo, and that is not incidental. A filled-in
deny-list names your employer, its vendor stack, its ticket prefixes and its
cluster naming — committing it would make the gate the most revealing file in
the tree, which is the failure it exists to prevent. `scrub-patterns.example`
ships with placeholders; your real one stays in `~/.config/knowledge/`. Add a
pattern the moment you notice a leak; the list is the memory.

## Configuration

`.knowledge.yml`, found by walking up from the working directory. Resolution
order for the store:

    --store flag  →  KNOWLEDGE_DIR  →  nearest .knowledge.yml  →  ~/.config/knowledge/config.yml

The user-level fallback matters more than it looks: agents frequently run in
scratch directories that sit under no project at all, so cwd traversal alone
files facts into whichever directory the shell happened to be in.

**Settings resolve separately from the store, and the store owns them.** Once the
store is located, its own `.knowledge.yml` is the settings base, and whichever
config named it overlays on top. Without that rule a config that merely *points*
at a store merges alone, leaving scope vocabulary, thresholds and the entity
guard at their defaults — so `--scope <yours>` starts being rejected and the
guard goes inert, silently, in exactly the sessions that run outside the store's
own tree. Overrides still work: any key the pointing config sets wins.

This is what makes a marker file at the root of a source tree useful. Drop one at
`~/src/github.com/acme/` naming the store, and every session under every repo in
that tree files facts into it, whatever the global default happens to be.

`knowledge config` prints the store, the file that decided it, and the settings
chain in merge order, so an unexpected value can be traced to a file.

## Clones and store discovery

The store is found by walking up from the working directory, which means a clone
placed under a directory that already has a `.knowledge.yml` inherits that store —
including this repo's own checkout, if you keep it somewhere already covered by a
marker. That is the intended mechanism, but it surprises people once. `knowledge
config` prints the resolved store and the file that decided it.

## Requirements

Python ≥3.11. The script declares its own dependencies inline and is meant to be
run with `uv`. `qmd` is optional — if present it adds a semantic check to
`knowledge add`; if absent that check is skipped with a warning.

## Tests

    ./run-tests.sh

73 tests, weighted toward the failures that actually occurred while this was
built rather than toward line coverage:

- a tokenizer that kept `per-cluster` whole and so never matched a paraphrase
  written `per cluster` — this silently halved a duplicate cluster
- a malformed config degrading to defaults, silently relocating the store and
  silently disabling the entity guard
- the entity guard vetoing an edge, *and* not vetoing when one side merely lists
  several identifiers as examples (the case that would break real merges)
- `derive_id` colliding between two facts that open with the same words, which
  is why merges pass `--id` explicitly
- candidates never appearing in the fact set — the safety property of the
  two-tier design, asserted rather than assumed
- recall firing on generic vocabulary alone, and recall returning unvetted
  candidate bodies — the two ways unattended retrieval goes wrong
- the test suite itself reading the developer's real `~/.config/knowledge`:
  `tune --write` wrote thresholds derived from synthetic fixtures into a live
  config before an autouse fixture isolated it

Each of those was checked by mutation: reverting the fix makes the corresponding
test fail. A test that passes against broken code is not evidence.

## Coverage is uneven, on purpose

The 79 tests cover `bin/knowledge`. `review/` has one test, `test_keys.mjs`,
which drives the real page in headless Chrome over CDP — it needs a Chrome binary
and a running server, so it does not run in the normal suite and it is the part
most likely to rot. Treat the CLI as tested and the review UI as exercised
daily by one person, which is not the same thing.

## Status

Extracted from a working single-user store, so the schema and the review flow are
exercised daily, but only by one person against one corpus. The thresholds in
particular are properties of that corpus — run `knowledge tune`.
