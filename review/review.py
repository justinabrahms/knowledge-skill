#!/usr/bin/env python3
"""One card, one decision. Serves plan.json as a review deck.

Reads the cards built by build_plan.py, shows one at a time, and executes the
answer through `knowledge` — confirm for what you keep, reject for what you
drop. Nothing is written until you answer a card, and nothing is written at all
until you clear the arming screen.

    python3 review.py [--port 8766]
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

# Per-store, not global: the deck is derived from one store's queue, so a global
# path collides the moment there is a second store. It sits beside `.usage.jsonl`
# and `.knowledge.yml` in the store itself — private by construction, and outside
# any repository, which is the property that matters.
def _plan_path() -> Path:
    raw = os.environ.get("KNOWLEDGE_REVIEW_PLAN")
    return Path(raw).expanduser() if raw else KDIR / ".review-plan.json"

HERE = Path(__file__).parent
LOCK = threading.Lock()

CARDS: list[dict] = []
POS = 0
SKIPPED: list[int] = []
DONE = 0
UNDO: list[dict] = []
ARMED = False
FILTER: dict = {"dim": None, "value": None}
CHOSEN = False        # False until a group (or "everything") has been picked
DIMS = ("provenance", "topic", "session", "repo")


def run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run([KNOWLEDGE, *args], capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout or p.stderr).strip()


def in_filter(c: dict) -> bool:
    """A card is in the current group. Topic matches on any of the card's topics,
    because a multi-fact card can legitimately span two of them."""
    dim, val = FILTER["dim"], FILTER["value"]
    if not dim:
        return True
    g = c.get("group") or {}
    if dim == "topic":
        return val in (g.get("topics") or [g.get("topic")])
    return g.get(dim) == val


def groups(dim: str) -> list[dict]:
    """Every value on one axis, with how much of it is still unanswered."""
    out: dict[str, dict] = {}
    for c in CARDS:
        vals = (c.get("group") or {}).get("topics") if dim == "topic" \
            else [(c.get("group") or {}).get(dim)]
        for v in (vals or ["?"]):
            e = out.setdefault(v or "?", {"value": v or "?", "total": 0, "left": 0})
            e["total"] += 1
            if not c.get("answered"):
                e["left"] += 1
    return sorted(out.values(), key=lambda e: (-e["left"], e["value"]))


def current() -> dict | None:
    """Next card: the deck in order, set-aside cards held back for a second pass."""
    global POS
    for _ in range(2):
        while POS < len(CARDS) and (CARDS[POS].get("answered") or POS in SKIPPED
                                    or not in_filter(CARDS[POS])):
            POS += 1
        if POS < len(CARDS):
            return CARDS[POS]
        # deck exhausted; fold the set-aside cards back in and go round once more
        if any(not CARDS[i].get("answered") and in_filter(CARDS[i]) for i in SKIPPED):
            SKIPPED.clear()
            POS = 0
            continue
        return None
    return None


def view() -> dict:
    c = current()
    left = sum(1 for x in CARDS if not x.get("answered"))
    return {
        "card": c,
        "left": left,
        "group_left": sum(1 for x in CARDS if not x.get("answered") and in_filter(x)),
        "filter": dict(FILTER),
        "chosen": CHOSEN,
        "dims": list(DIMS),
        "groups": {d: groups(d) for d in DIMS},
        "skipped": len([i for i in SKIPPED if not CARDS[i].get("answered") and in_filter(CARDS[i])]),
        "done": DONE,
        "undo_depth": len(UNDO),
        "store": str(KDIR),
        "armed": ARMED,
        "total": len(CARDS),
    }


def act(body: dict) -> dict:
    global DONE
    c = current()
    if c is None:
        return {"ok": False, "message": "deck is empty"}
    action = body.get("action")

    if action == "skip":
        global POS
        if POS not in SKIPPED:
            SKIPPED.append(POS)
        POS += 1
        return {"ok": True, "message": "skipped", "advance": True}

    texts = body.get("texts") or [k["text"] for k in c["keep"]]
    # Each kept fact is an independent store entry with its own id, confidence and
    # verification lifecycle, so the card is answered per fact, not as a block.
    include = body.get("include") or [True] * len(c["keep"])
    NOT_DURABLE = "reviewed 2026-08-24: not durable enough to store"
    # `knowledge confirm` deletes the candidate file, so snapshot it first —
    # otherwise undo can put the fact back but not the queue entry, and
    # re-answering the card would fail on a missing candidate.
    step: dict = {"confirmed": [], "rejected": [], "index": CARDS.index(c), "snapshots": {}}

    def snapshot(pid: str) -> None:
        src = KDIR / "pending" / f"{pid}.yml"
        if src.exists():
            step["snapshots"][pid] = src.read_text()

    def stored_id(out: str, fallback: str) -> str:
        words = out.replace("\n", " ").split()
        for j, w in enumerate(words):
            if w.lower().startswith("confirmed") and j + 1 < len(words):
                return words[j + 1]
        return fallback

    drops: list[tuple[str, str]] = []   # (candidate id, rejection reason)

    if action == "keep":
        if not any(include):
            return {"ok": False, "message": "no fact selected — press x to reject the whole card"}
        for i, keep in enumerate(c["keep"]):
            if not include[i]:
                continue
            text = (texts[i] if i < len(texts) else keep["text"]).strip()
            if not text:
                return {"ok": False, "message": f"fact #{i+1} has no text"}
            snapshot(keep["from_id"])
            code, out = run(["confirm", keep["from_id"], "--text", text,
                             "--topic", keep["topic"], "--scope", keep["scope"],
                             "--confidence", keep["confidence"],
                             "--id", keep.get("store_id") or keep["from_id"]])
            if code != 0:
                UNDO.append(step)   # keep whatever landed so it can be reversed
                return {"ok": False, "message": f"{keep['from_id']}: {out}"}
            step["confirmed"].append(stored_id(out, keep["from_id"]))
        dup_reason = f"duplicate of {step['confirmed'][0]}" if step["confirmed"] else "duplicate"
        drops = [(pid, dup_reason) for pid in c["drop"]]
        drops += [(keep["from_id"], NOT_DURABLE)
                  for i, keep in enumerate(c["keep"]) if not include[i]]
    elif action == "unmerge":
        # Refuse the merge: store every candidate as its own fact, in its own
        # words, under its own id. Nothing is rejected.
        for m in c["members"]:
            snapshot(m["id"])
            code, out = run(["confirm", m["id"]])
            if code != 0:
                UNDO.append(step)
                return {"ok": False, "message": f"{m['id']}: {out}"}
            step["confirmed"].append(stored_id(out, m["id"]))
    elif action == "drop":
        reason = (body.get("reason") or "").strip() or NOT_DURABLE
        drops = [(pid, reason) for pid in c["consumes"]]
    else:
        return {"ok": False, "message": f"unknown action {action}"}

    failed = []
    for pid, why in drops:
        code, out = run(["reject", pid, "--reason", why])
        if code == 0:
            step["rejected"].append(pid)
        else:
            failed.append(f"{pid}: {out}")

    c["answered"] = action
    idx = CARDS.index(c)
    if idx in SKIPPED:
        SKIPPED.remove(idx)
    UNDO.append(step)
    DONE += 1
    n_kept, n_dropped = len(step["confirmed"]), len(step["rejected"])
    if action == "drop":
        msg = f"rejected {n_dropped}"
    elif action == "unmerge":
        msg = f"kept separate: {n_kept} fact(s) stored"
    else:
        msg = f"stored {n_kept}, rejected {n_dropped}"
    if failed:
        msg += " | failed: " + "; ".join(failed[:3])
    return {"ok": not failed, "message": msg, "advance": True}


def undo() -> dict:
    """Reverse the last answered card: delete what it stored, requeue what it dropped."""
    global DONE, POS
    if not UNDO:
        return {"ok": False, "message": "nothing to undo"}
    step = UNDO.pop()
    notes = []
    for pid in step["rejected"]:
        src = KDIR / "pending/rejected" / f"{pid}.yml"
        if not src.exists():
            notes.append(f"{pid} not in rejected log")
            continue
        lines = [l for l in src.read_text().splitlines()
                 if not l.startswith(("rejected_at:", "rejection_reason:"))]
        (KDIR / "pending" / f"{pid}.yml").write_text("\n".join(lines) + "\n")
        src.unlink()
    for fid in step["confirmed"]:
        f = KDIR / f"{fid}.md"
        if f.exists():
            f.unlink()
        else:
            notes.append(f"{fid}.md already gone")
    for pid, blob in step.get("snapshots", {}).items():
        dest = KDIR / "pending" / f"{pid}.yml"
        if not dest.exists():
            dest.write_text(blob)
    CARDS[step["index"]].pop("answered", None)
    POS = min(POS, step["index"])
    DONE = max(0, DONE - 1)
    n_back = len(step["rejected"]) + len(step.get("snapshots", {}))
    msg = f"undone: {n_back} candidate(s) back in the queue, {len(step['confirmed'])} stored fact(s) removed"
    if notes:
        msg += " | " + "; ".join(notes)
    return {"ok": True, "message": msg}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            self._send(200, (HERE / "card.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/card"):
            with LOCK:
                self._json(view())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        global ARMED, CHOSEN, POS
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "message": "bad json"}, 400)
        route = self.path.split("?")[0]
        with LOCK:
            if route == "/api/select":
                dim = body.get("dim")
                FILTER["dim"] = dim if dim in DIMS else None
                FILTER["value"] = body.get("value") if FILTER["dim"] else None
                CHOSEN = True
                POS = 0
                SKIPPED.clear()
                self._json({"ok": True, "message": "group selected"})
            elif route == "/api/unselect":
                CHOSEN = False
                self._json({"ok": True, "message": "back to groups"})
            elif route == "/api/arm":
                ARMED = True
                self._json({"ok": True, "message": "armed"})
            elif route == "/api/act":
                if not ARMED:
                    return self._json({"ok": False, "message": "not armed"})
                self._json(act(body))
            elif route == "/api/undo":
                self._json(undo())
            else:
                self._json({"ok": False, "message": "not found"}, 404)


def main() -> int:
    global CARDS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()

    plan = _plan_path()
    if not plan.exists():
        print("no plan.json — run: python3 build_plan.py", file=sys.stderr)
        return 1
    data = json.loads(plan.read_text())
    CARDS = data["cards"]
    print(f"store: {KDIR}")
    print(f"deck:  {len(CARDS)} cards ({data['cluster_cards']} merges collapsing "
          f"{data['collapsed']} candidates, {data['single_cards']} singles)")
    print(f"open:  http://127.0.0.1:{args.port}/    ctrl-c to stop")
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
