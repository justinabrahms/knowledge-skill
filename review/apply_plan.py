#!/usr/bin/env python3
"""Apply the plan's dedup cards in bulk, reversibly.

Only touches cards flagged `dedup` — groups that are genuinely restatements of
one fact. Cards that merely clustered on shared vocabulary, and every single
candidate, are left for a human.

Writes applied-<n>.json recording each confirm (with a snapshot of the candidate
file that `knowledge confirm` deletes) and each reject, so --rollback restores
the exact prior state.

    python3 apply_plan.py                    # dry run
    python3 apply_plan.py --apply
    python3 apply_plan.py --rollback applied-1.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Resolve the CLI and the store the same way the CLI itself does, rather than
# hardcoding either: this UI is a client, and a second copy of the precedence
# rules is a second thing to get out of sync.
KNOWLEDGE = os.environ.get("KNOWLEDGE_BIN") or str(Path(__file__).resolve().parent.parent / "bin" / "knowledge")


def _store_dir() -> Path:
    if os.environ.get("KNOWLEDGE_DIR"):
        return Path(os.environ["KNOWLEDGE_DIR"]).expanduser()
    out = subprocess.run([KNOWLEDGE, "config", "--path"],
                         capture_output=True, text=True, timeout=60)
    return Path(out.stdout.strip()).expanduser()


KDIR = _store_dir()
HERE = Path(__file__).parent


def run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run([KNOWLEDGE, *args], capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout or p.stderr).strip()


def stored_id(out: str, fallback: str) -> str:
    words = out.replace("\n", " ").split()
    for i, w in enumerate(words):
        if w.lower().startswith("confirmed") and i + 1 < len(words):
            return words[i + 1]
    return fallback


def apply(cards: list[dict], dry: bool) -> dict:
    log = {"store": str(KDIR), "steps": []}
    for c in cards:
        print(f"\n{'would merge' if dry else 'merging'}: {c['title']}")
        print(f"  {len(c['consumes'])} candidates -> {len(c['keep'])} fact(s), "
              f"{len(c['drop'])} rejected")
        step = {"title": c["title"], "confirmed": [], "rejected": [], "snapshots": {}}
        ok = True
        for keep in c["keep"]:
            print(f"  keep  [{keep['topic']}/{keep['scope']}/{keep['confidence']}] "
                  f"{keep['text'][:88]}...")
            if dry:
                continue
            src = KDIR / "pending" / f"{keep['from_id']}.yml"
            if src.exists():
                step["snapshots"][keep["from_id"]] = src.read_text()
            code, out = run(["confirm", keep["from_id"], "--text", keep["text"],
                             "--topic", keep["topic"], "--scope", keep["scope"],
                             "--confidence", keep["confidence"],
                             "--id", keep.get("store_id") or keep["from_id"]])
            if code != 0:
                print(f"  !! confirm failed for {keep['from_id']}: {out}")
                ok = False
                break
            step["confirmed"].append(stored_id(out, keep["from_id"]))
        if not ok:
            log["steps"].append(step)
            print("  aborting this card; earlier cards stay applied")
            break
        reason = (f"duplicate of {step['confirmed'][0]}" if step["confirmed"]
                  else f"duplicate, merged into '{c['title']}'")
        for pid in c["drop"]:
            if dry:
                continue
            code, out = run(["reject", pid, "--reason", reason])
            if code == 0:
                step["rejected"].append(pid)
            else:
                print(f"  !! reject failed for {pid}: {out}")
        if not dry:
            log["steps"].append(step)
    return log


def rollback(path: Path) -> int:
    log = json.loads(path.read_text())
    if log.get("store") != str(KDIR):
        print(f"refusing: log was written against {log.get('store')}, not {KDIR}", file=sys.stderr)
        return 2
    back = removed = 0
    for step in reversed(log["steps"]):
        for pid in step["rejected"]:
            src = KDIR / "pending/rejected" / f"{pid}.yml"
            if not src.exists():
                continue
            lines = [l for l in src.read_text().splitlines()
                     if not l.startswith(("rejected_at:", "rejection_reason:"))]
            (KDIR / "pending" / f"{pid}.yml").write_text("\n".join(lines) + "\n")
            src.unlink()
            back += 1
        for fid in step["confirmed"]:
            f = KDIR / f"{fid}.md"
            if f.exists():
                f.unlink()
                removed += 1
        for pid, blob in step.get("snapshots", {}).items():
            dest = KDIR / "pending" / f"{pid}.yml"
            if not dest.exists():
                dest.write_text(blob)
                back += 1
    print(f"rolled back: {back} candidate(s) requeued, {removed} stored fact(s) removed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--rollback", metavar="LOG", help="reverse a previous --apply")
    args = ap.parse_args()

    if args.rollback:
        return rollback(Path(args.rollback))

    plan = json.loads((HERE / "plan.json").read_text())
    cards = [c for c in plan["cards"] if c.get("dedup") and c["consumes"]]
    if not cards:
        print("no dedup cards left to apply")
        return 0

    print(f"store: {KDIR}")
    print(f"{len(cards)} dedup card(s): {sum(len(c['consumes']) for c in cards)} candidates "
          f"-> {sum(len(c['keep']) for c in cards)} facts, "
          f"{sum(len(c['drop']) for c in cards)} rejections")
    log = apply(cards, dry=not args.apply)

    if not args.apply:
        print("\ndry run — nothing written. re-run with --apply")
        return 0
    n = 1
    while (HERE / f"applied-{n}.json").exists():
        n += 1
    out = HERE / f"applied-{n}.json"
    out.write_text(json.dumps(log, indent=2))
    kept = sum(len(s["confirmed"]) for s in log["steps"])
    drop = sum(len(s["rejected"]) for s in log["steps"])
    print(f"\nstored {kept} fact(s), rejected {drop} candidate(s)")
    print(f"rollback with: python3 apply_plan.py --rollback {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
