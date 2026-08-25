#!/usr/bin/env python3
"""Build plan.json: one card per decision, merged text pre-written.

Cards come in two kinds. Cluster cards are hand-authored below — a human read
all the members, resolved or explicitly refused to resolve their contradictions,
and wrote the text that should actually be stored. Singleton cards are generated
from whatever is left in the queue.

A card is one yes/no: `keep` is the fact(s) to store, `drop` is every candidate
the card consumes. Answering the card empties that part of the queue.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# Client, not a second implementation: resolve the CLI the same way the other
# review scripts do so all three agree on which binary is authoritative.
KNOWLEDGE = os.environ.get("KNOWLEDGE_BIN") or str(Path(__file__).resolve().parent.parent / "bin" / "knowledge")


def _store_dir() -> Path:
    """Resolve the store exactly as the CLI does, rather than re-implementing
    the precedence rules and drifting from them."""
    if os.environ.get("KNOWLEDGE_DIR"):
        return Path(os.environ["KNOWLEDGE_DIR"]).expanduser()
    out = subprocess.run([KNOWLEDGE, "config", "--path"],
                         capture_output=True, text=True, timeout=60)
    return Path(out.stdout.strip()).expanduser()


KDIR = _store_dir()
# Per-store, not global: the deck is derived from one store's queue, so a global
# path collides the moment there is a second store. It sits beside `.usage.jsonl`
# and `.knowledge.yml` in the store itself — private by construction, and outside
# any repository, which is the property that matters.
def _plan_path() -> Path:
    raw = os.environ.get("KNOWLEDGE_REVIEW_PLAN")
    return Path(raw).expanduser() if raw else KDIR / ".review-plan.json"

# Scope for a candidate that never recorded one. Read from the store's config so
# a singleton card can't file facts under another organisation's vocabulary.
def _default_scope() -> str:
    try:
        out = subprocess.run([KNOWLEDGE, "config"], capture_output=True, text=True, timeout=60)
        for line in out.stdout.splitlines():
            if line.startswith("scopes"):
                return line.split()[1].rstrip(",")
    except Exception:
        pass
    return "personal"


DEFAULT_SCOPE = _default_scope()


def k(text, topic, scope, confidence, from_id, store_id=None):
    """store_id is the id the fact lands under. Without it the CLI derives one
    from the leading words of the body, which collides between facts that open
    the same way and reads badly in Obsidian."""
    return {"text": " ".join(text.split()), "topic": topic, "scope": scope,
            "confidence": confidence, "from_id": from_id,
            "store_id": store_id or from_id}


# Each card: title, note (what the duplicates disagreed about), keep[], drop[]
# The hand-authored deck describes one specific store's contents, so it is not
# part of the tool, and it is not global either — it lives in the store it
# describes. Loaded from KNOWLEDGE_REVIEW_CARDS or <store>/.review-cards.py, and
# defaults to empty: with no deck, every candidate gets its own singleton card.
def _load_cards() -> list[dict]:
    raw = os.environ.get("KNOWLEDGE_REVIEW_CARDS")
    path = Path(raw).expanduser() if raw else _store_dir() / ".review-cards.py"
    if not path.is_file():
        return []
    ns: dict = {"k": k, "__file__": str(path)}
    exec(compile(path.read_text(), str(path), "exec"), ns)
    return list(ns.get("CARDS") or [])


CARDS: list[dict] = _load_cards()


def repo_of(m: dict) -> str:
    """The repo the candidate is about, as `propose` recorded it. Never guessed
    from the directory name — `tmp/maker-time` is a scratch dir, not a repo, and
    labelling it one made the axis look more useful than it was."""
    repo = str(m.get("repo") or "").strip()
    return repo.split("/", 1)[-1] if repo else "(no repo)"


def group_of(members: list[dict], keeps: list[dict]) -> dict:
    """Every axis the deck can be sliced on. A card belongs to one value per axis;
    where its members disagree the value is 'mixed' so the card still shows up
    somewhere rather than vanishing from every group."""
    def one(vals: list[str], default: str) -> str:
        u = sorted({v for v in vals if v})
        return u[0] if len(u) == 1 else ("mixed" if u else default)
    return {
        "topics": sorted({kk["topic"] for kk in keeps}),
        "topic": one([kk["topic"] for kk in keeps], "workflow"),
        "session": one([str(m.get("source") or "") for m in members], "unknown"),
        "provenance": one([str(m.get("provenance") or "") for m in members], "inferred"),
        "repo": one([repo_of(m) for m in members], "(no repo)"),
    }


def live_queue() -> dict[str, dict]:
    out = subprocess.run([KNOWLEDGE, "dupes", "--json", "--min-size", "1"],
                         capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return {m["id"]: m for c in data["clusters"] for m in c["members"] if m["kind"] == "pending"}


def main() -> int:
    # Name the store before doing anything. This file lives in a repo that may
    # itself sit under a `.knowledge.yml` marker, so the store it resolves to is
    # not always the one you meant — and a deck built for the wrong queue looks
    # exactly like an empty queue.
    print(f"store  {KDIR}")
    queue = live_queue()
    cards: list[dict] = []
    claimed: set[str] = set()
    problems: list[str] = []

    for i, card in enumerate(CARDS):
        ids = [kk["from_id"] for kk in card["keep"]] + list(card["drop"])
        present = [x for x in ids if x in queue]
        if not present:
            continue                      # already answered in an earlier session
        missing = [x for x in ids if x not in queue]
        if missing:
            problems.append(f"card {i} '{card['title']}' partially consumed, missing: {missing}")
        claimed.update(present)
        cards.append({
            "kind": "cluster",
            # False where the group is distinct claims that merely clustered on
            # shared vocabulary — nothing to merge, so a bulk pass must skip it.
            "dedup": card.get("dedup", True),
            "title": card["title"],
            "note": card["note"],
            "keep": [kk for kk in card["keep"] if kk["from_id"] in queue],
            "drop": [d for d in card["drop"] if d in queue],
            "consumes": present,
            "members": [{key: queue[x][key] for key in
                         ("id", "text", "provenance", "confidence", "scope", "topic",
                          "evidence", "source", "proposed_at", "repo", "cwd")} for x in present],
            "group": group_of([queue[x] for x in present], card["keep"]),
        })

    for pid, m in sorted(queue.items()):
        if pid in claimed:
            continue
        cards.append({
            "kind": "single",
            "dedup": False,
            "title": pid,
            "note": "",
            "keep": [k(m["text"], m["topic"] or "workflow", m["scope"] or DEFAULT_SCOPE,
                       m["confidence"] or "low", pid)],
            "drop": [],
            "consumes": [pid],
            "members": [{key: m[key] for key in
                         ("id", "text", "provenance", "confidence", "scope", "topic",
                          "evidence", "source", "proposed_at", "repo", "cwd")}],
            "group": group_of([m], [k(m["text"], m["topic"] or "workflow",
                                      m["scope"] or DEFAULT_SCOPE,
                                      m["confidence"] or "low", pid)]),
        })

    payload = {"cards": cards,
               "queue_size": len(queue),
               "cluster_cards": sum(1 for c in cards if c["kind"] == "cluster"),
               "single_cards": sum(1 for c in cards if c["kind"] == "single"),
               "collapsed": len(claimed),
               "facts_if_all_kept": sum(len(c["keep"]) for c in cards)}
    _plan_path().write_text(json.dumps(payload, indent=2))

    for p in problems:
        print("WARN:", p)
    print(f"queue {payload['queue_size']} candidates")
    print(f"  {payload['cluster_cards']} cluster cards collapsing {payload['collapsed']} candidates"
          f" -> {sum(len(c['keep']) for c in cards if c['kind']=='cluster')} facts")
    print(f"  {payload['single_cards']} single cards")
    print(f"  {len(cards)} decisions total (was {payload['queue_size']})")
    return 1 if problems else 0
if __name__ == "__main__": raise SystemExit(main())
