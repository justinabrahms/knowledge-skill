"""Retrieval quality gate.

The unit suite tests `recall` the function. This tests `recall` the *answer*:
given a fixed corpus and a labelled question, does the fact that answers it come
back? That is the layer the repo-filter regression lived in -- 104 unit tests
stayed green while recall from a checkout returned nothing, because every one of
them asserted on machinery rather than on whether a question got answered.

Two kinds of check, deliberately graded differently:

  - Hard assertions. A refuted, superseded, invalidated or out-of-window fact
    coming back, or recall firing on pure filler, is a correctness bug. These
    fail on their own, regardless of the aggregate.
  - A ratchet. recall@3 and MRR are compared against `eval-baseline.json` with a
    small tolerance. No absolute bar is defensible for a corpus this size, but
    "this change made retrieval worse" always is. Regenerate deliberately with
    UPDATE_EVAL_BASELINE=1 ./run-tests.sh, and treat a drop you are about to
    bless as a thing to explain in the commit message.

The corpus is invented from end to end. It is not an anonymised export: a public
repository should not carry scrubbed private notes, and a scrubber is not a thing
to stake that on. Facts share vocabulary on purpose -- a corpus where every fact
is lexically distinct scores 1.0 forever and detects nothing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from conftest import CLI, add_fact, k

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASELINE = FIXTURES / "eval-baseline.json"
# Enough to absorb tie-order jitter, tight enough that losing one answer in a
# corpus this size trips it.
TOLERANCE = 0.02
K = 3


def _rows(name: str) -> list[dict]:
    return [r for r in json.loads((FIXTURES / name).read_text()) if not r.get("skip")]


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return _rows("eval-queries.json")


@pytest.fixture
def corpus_store(store):
    """The eval corpus, loaded into a throwaway store."""
    for row in _rows("eval-corpus.json"):
        row = dict(row)
        row.pop("_comment", None)
        add_fact(store, row.pop("id"), row.pop("text"), **row)
    return store


def recall_ids(case: dict, monkeypatch) -> list[str]:
    """Ids `recall` returns for one case, as the hook would see them."""
    from typer.testing import CliRunner

    monkeypatch.setattr(k, "detect_repo", lambda cwd="": case.get("cwd", ""))
    monkeypatch.setattr(k, "repo_checkout", lambda repo, cwd="": None)
    res = CliRunner().invoke(
        k.app, ["recall", case["query"], "--limit", str(K), "--json"])
    assert res.exit_code == 0, f"{case['id']}: {res.exception!r}"
    return [h["id"] for h in json.loads(res.stdout)["hits"]]


def score(cases: list[dict], monkeypatch) -> dict:
    """recall@K and MRR over the cases that assert a positive."""
    recalls: list[float] = []
    reciprocal: list[float] = []
    for case in cases:
        expected = case.get("expect") or []
        if not expected:
            continue
        got = recall_ids(case, monkeypatch)
        recalls.append(len(set(got) & set(expected)) / len(expected))
        rank = next((i + 1 for i, fid in enumerate(got) if fid in expected), 0)
        reciprocal.append(1.0 / rank if rank else 0.0)
    return {
        "cases": len(recalls),
        f"recall_at_{K}": round(sum(recalls) / len(recalls), 4),
        "mrr": round(sum(reciprocal) / len(reciprocal), 4),
    }


class TestRetrievalCorrectness:
    """Hard failures. These do not participate in the ratchet."""

    def test_no_case_returns_an_unretrievable_fact(self, corpus_store, cases, monkeypatch):
        offences = []
        for case in cases:
            for fid in case.get("must_not") or []:
                if fid in recall_ids(case, monkeypatch):
                    offences.append(f"{case['id']} returned {fid}")
        assert not offences, "refuted/superseded/expired facts surfaced: " + "; ".join(offences)

    def test_filler_queries_stay_silent(self, corpus_store, cases, monkeypatch):
        noisy = [case["id"] for case in cases
                 if case.get("expect_empty") and recall_ids(case, monkeypatch)]
        assert not noisy, f"recall fired on filler: {noisy}"

    def test_ranking_pins_hold(self, corpus_store, cases, monkeypatch):
        """Where a case names the answer that must come first, it must come
        first. Presence-only scoring hides a ranking regression completely."""
        wrong = []
        for case in cases:
            want = case.get("expect_first")
            if not want:
                continue
            got = recall_ids(case, monkeypatch)
            if not got or got[0] != want:
                wrong.append(f"{case['id']}: wanted {want}, got {got[:1] or ['nothing']}")
        assert not wrong, "ranking regressions: " + "; ".join(wrong)

    def test_a_cross_repo_answer_survives_being_asked_from_elsewhere(
            self, corpus_store, cases, monkeypatch):
        """The named regression, kept as its own test so it cannot be lost in an
        aggregate: the answer is tagged with one repo and asked from another."""
        case = next(c for c in cases if c["id"] == "cross-repo-slo-gate")
        assert case["expect"][0] in recall_ids(case, monkeypatch)


class TestRetrievalRatchet:
    def test_quality_has_not_regressed(self, corpus_store, cases, monkeypatch):
        current = score(cases, monkeypatch)

        if os.getenv("UPDATE_EVAL_BASELINE") == "1":
            BASELINE.write_text(json.dumps(current, indent=2) + "\n")
            pytest.skip(f"baseline rewritten: {current}")

        assert BASELINE.exists(), (
            "no eval baseline; create one with UPDATE_EVAL_BASELINE=1 ./run-tests.sh")
        base = json.loads(BASELINE.read_text())

        assert current["cases"] == base["cases"], (
            f"case count changed ({base['cases']} -> {current['cases']}); "
            "the scores are not comparable. Rebaseline deliberately.")

        drops = {m: (base[m], current[m]) for m in (f"recall_at_{K}", "mrr")
                 if current[m] < base[m] - TOLERANCE}
        assert not drops, (
            "retrieval quality regressed: "
            + "; ".join(f"{m} {was} -> {now}" for m, (was, now) in drops.items())
            + ". If this is intended, rebaseline with UPDATE_EVAL_BASELINE=1 and say why.")


class TestUnverifiedLeads:
    """The second tier must not leak into the first, or exceed its budget.

    Leads are why the gate matters more, not less: they ride along on every
    prompt, so they have to obey the same relevance floor and a hard cap, and
    they must never appear where a confirmed fact does.
    """

    @pytest.fixture
    def with_leads(self, corpus_store):
        from datetime import date
        today = date.today().isoformat()
        for i in range(4):
            pid = f"ledger-lead-{i}"
            (corpus_store / "pending" / f"{pid}.yml").write_text(yaml.safe_dump({
                "id": pid, "topic": "t", "scope": "org", "confidence": "low",
                "provenance": "inferred", "proposed_at": today,
                "text": f"The ledger service on-call rotation is handled by team {i}.",
                "validator": {"type": "git-path", "path": "x"}}))
            k.append_jsonl(k.VALIDATION_LOG_NAME, {
                "assertion_id": pid, "candidate": True, "outcome": "supported",
                "observed_at": f"{today}T00:00:00+00:00", "validator": "git-path",
                "evidence": "r@abc:x", "detail": "path exists"})
        return corpus_store

    def _recall(self, query, monkeypatch):
        from typer.testing import CliRunner
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "")
        monkeypatch.setattr(k, "repo_checkout", lambda repo, cwd="": None)
        res = CliRunner().invoke(k.app, ["recall", query, "--json"])
        assert res.exit_code == 0, res.exception
        return json.loads(res.stdout)

    QUERY = "which team owns the ledger service and its on-call rotation"

    def test_leads_never_appear_among_confirmed_hits_and_stay_capped(self, with_leads, monkeypatch):
        out = self._recall(self.QUERY, monkeypatch)
        assert "ledger-owned-by-lantern" in [h["id"] for h in out["hits"]]
        assert not any(h["id"].startswith("ledger-lead") for h in out["hits"])
        assert 0 < len(out["leads"]) <= 2

    def test_filler_stays_silent_with_leads_present(self, with_leads, cases, monkeypatch):
        for case in cases:
            if case.get("expect_empty"):
                out = self._recall(case["query"], monkeypatch)
                assert out["hits"] == [] and out["leads"] == [], case["id"]

    def test_confirmed_answers_are_unchanged_by_leads(self, with_leads, cases, monkeypatch):
        """Leads are additive. The ratcheted metrics must not move."""
        assert score(cases, monkeypatch) == {
            k_: v for k_, v in json.loads(BASELINE.read_text()).items()}


class TestRecallHookContract:
    """The hook, not the CLI underneath it.

    This repo has already shipped a recall hook that read the prompt from an
    environment variable the harness does not set, so it silently recalled
    nothing on every message while looking perfectly healthy. Nothing in the
    unit suite could see that, and it costs no model spend to check.
    """

    HOOK = Path(__file__).resolve().parent.parent / "hooks" / "knowledge-recall.sh"

    def _run(self, payload: dict, store: Path, env: dict | None = None):
        import subprocess
        return subprocess.run(
            ["bash", str(self.HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, timeout=30,
            env={**os.environ, "KNOWLEDGE_DIR": str(store),
                 "KNOWLEDGE_BIN": str(CLI),
                 **(env or {})})

    def test_the_hook_reads_the_prompt_from_stdin_and_finds_a_fact(self, corpus_store):
        res = self._run({"prompt": "which team owns the ledger service and its on-call rotation",
                         "session_id": "s-1"}, corpus_store)
        assert res.returncode == 0, res.stderr
        assert "ledger-owned-by-lantern" in res.stdout, res.stdout or res.stderr

    def test_the_hook_prints_leads_in_their_own_caveated_block(self, corpus_store):
        from datetime import date
        today = date.today().isoformat()
        (corpus_store / "pending" / "ledger-lead.yml").write_text(yaml.safe_dump({
            "id": "ledger-lead", "topic": "t", "scope": "org", "confidence": "low",
            "provenance": "inferred", "proposed_at": today,
            "text": "The ledger service on-call rotation is handled by a guess team.",
            "validator": {"type": "git-path", "path": "x"}}))
        (corpus_store / k.VALIDATION_LOG_NAME).write_text(json.dumps({
            "assertion_id": "ledger-lead", "candidate": True, "outcome": "supported",
            "observed_at": f"{today}T00:00:00+00:00", "validator": "git-path",
            "evidence": "r@abc:x", "detail": "path exists"}) + "\n")
        res = self._run({"prompt": "which team owns the ledger service and its on-call rotation",
                         "session_id": "s-3"}, corpus_store)
        facts, _, leads = res.stdout.partition("# Unverified leads")
        assert "ledger-owned-by-lantern" in facts and "ledger-lead" not in facts
        assert "ledger-lead" in leads and "NOT established" in leads

    def test_the_hook_attributes_the_session(self, corpus_store):
        self._run({"prompt": "which team owns the ledger service and its on-call rotation",
                   "session_id": "s-attributed"}, corpus_store)
        rows = [json.loads(x) for x in
                (corpus_store / k.USAGE_LOG_NAME).read_text().splitlines() if x.strip()]
        assert rows[-1]["session_id"] == "s-attributed"
        assert rows[-1]["agent"] == "claude"

    def test_the_hook_is_silent_on_filler(self, corpus_store):
        res = self._run({"prompt": "thanks, can you also update the test files today",
                         "session_id": "s-2"}, corpus_store)
        assert res.returncode == 0 and res.stdout.strip() == ""

    def test_the_hook_never_blocks_a_prompt(self, corpus_store):
        """A hook that exits non-zero on a bad payload costs the user their
        message. Silence is the only acceptable failure."""
        for payload in ({}, {"prompt": ""}, {"prompt": "hi"}, {"prompt": "/some-command"}):
            res = self._run(payload, corpus_store)
            assert res.returncode == 0, f"{payload} -> rc={res.returncode} {res.stderr}"
            assert res.stdout.strip() == ""
