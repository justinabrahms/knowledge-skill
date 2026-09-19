"""Tests for the knowledge CLI.

Weighted deliberately toward the failures that actually happened while this tool
was being built, rather than toward line coverage:

  - a tokenizer that kept `per-cluster` whole, so it never matched a paraphrase
    written `per cluster` (this one silently halved a duplicate cluster)
  - metadata keyed by bare id, which merged a stored fact with a queued
    candidate of the same name and hid the collision instead of showing it
  - `confirm` consuming the candidate file, so undo could restore the fact but
    not the queue entry
  - id derivation colliding between two facts that open with the same words
  - a malformed config degrading to defaults, silently relocating the store and
    silently disabling the entity guard

Run: ./run-tests.sh
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import json
import frontmatter

import pytest
import yaml

from conftest import CLI, add_fact, k, plain  # noqa: F401  (re-exported for tests)

TOPIC_HOOK = Path(__file__).resolve().parent.parent / "hooks" / "knowledge-inject-topics.sh"

def test_topic_hook_does_not_nag_about_unreviewed_candidates(tmp_path):
    """Pending candidates are excluded from unattended retrieval by design."""
    store = tmp_path / "store"
    (store / "pending").mkdir(parents=True)
    (store / "pending" / "candidate.yml").write_text("text: unreviewed\n")
    knowledge_bin = tmp_path / "knowledge"
    knowledge_bin.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  topics) echo 'testing (1)' ;;\n"
        f"  config) echo '{store}' ;;\n"
        "esac\n"
    )
    knowledge_bin.chmod(0o755)

    result = subprocess.run(
        ["bash", str(TOPIC_HOOK)],
        env={**os.environ, "KNOWLEDGE_BIN": str(knowledge_bin)},
        text=True,
        capture_output=True,
        check=True,
    )

    assert "candidate assertion" not in result.stdout
    assert "await review" not in result.stdout


# ---------------------------------------------------------------- fixtures

def write_config(store: Path, **overrides) -> Path:
    cfg = {"store": ".", "scopes": ["org", "personal"], "prefixed_scopes": ["repo", "task"]}
    cfg.update(overrides)
    p = store / ".knowledge.yml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    k._CONFIG = None
    k.load_config(reload=True)
    return p


def add_candidate(store: Path, pid: str, text: str, **fields) -> Path:
    rec = {"id": pid, "text": text, "topic": fields.get("topic", "t"),
           "scope": fields.get("scope", "org"), "confidence": fields.get("confidence", "low"),
           "provenance": fields.get("provenance", "inferred"),
           "evidence": fields.get("evidence", ""), "source": "test",
           "proposed_at": "2026-01-01"}
    for key, value in fields.items():
        if key not in {"topic", "scope", "confidence", "provenance", "evidence"}:
            rec[key] = value
    p = store / "pending" / f"{pid}.yml"
    p.write_text(yaml.safe_dump(rec, sort_keys=False))
    return p


# ------------------------------------------------------- tokenizer / similarity

class TestTokenizer:
    def test_compound_also_yields_parts_and_squashed_form(self):
        """The bug: `per-cluster` stayed whole and never matched `per cluster`."""
        toks = set(k.content_tokens("Argo CD runs per-cluster"))
        assert "per-cluster" in toks, "compound must survive intact for entity names"
        assert "cluster" in toks, "compound must also yield its parts"
        assert "percluster" in toks, "squashed form lets argo-cd match argocd"

    def test_hyphenated_matches_spaced_paraphrase(self):
        a = k.content_tokens("Argo CD runs per-cluster, not as a central instance")
        b = k.content_tokens("ArgoCD is deployed once per cluster rather than centrally")
        assert set(a) & set(b), "a paraphrase must share tokens with its original"

    def test_stopwords_and_short_tokens_dropped(self):
        toks = set(k.content_tokens("the a an of to in is it"))
        assert toks == set()

    def test_service_name_survives_whole(self):
        assert "aks-prod-weu-01" in k.content_tokens("cluster aks-prod-weu-01 is primary")


class TestSimilarity:
    def test_identical_text_scores_one(self):
        t = "deploys go out through gitops"
        idf = k._idf([k.content_tokens(t)])
        v = k._vec(k.content_tokens(t), idf)
        assert k._cos(v, v) == pytest.approx(1.0, abs=1e-6)

    def test_unrelated_text_scores_low(self):
        docs = ["deploys go out through gitops", "the coffee machine is on floor three"]
        idf = k._idf([k.content_tokens(d) for d in docs])
        va, vb = (k._vec(k.content_tokens(d), idf) for d in docs)
        assert k._cos(va, vb) < 0.1

    def test_empty_text_is_safe(self):
        idf = k._idf([k.content_tokens("anything")])
        assert k._cos(k._vec(k.content_tokens(""), idf), k._vec([], idf)) == 0.0

    def test_jaccard_misses_paraphrase(self):
        """Why cosine exists: token overlap does not survive rewording."""
        a = "Team calendars are readable org-wide via the API"
        b = "Every employee calendar can be read across the organisation"
        assert k.similarity(a, b) < 0.75


# --------------------------------------------------------------- entity guard

JIRA = [{"name": "jira", "lowercase": False,
         "patterns": [r"\b([A-Z]{2,6})\s+project\b", r"\b([A-Z]{2,6})-\d+\b"]}]
EMAIL = [{"name": "email", "lowercase": True,
          "patterns": [r"\b([a-z0-9][a-z0-9._-]{1,40})@"]}]


class TestEntityGuard:
    def test_inert_without_config(self, store):
        write_config(store, entities=[])
        assert k.entity_conflict("the DPA project", "the SEC project") is None

    def test_blocks_single_differing_identifier(self, store):
        write_config(store, entities=JIRA)
        c = k.entity_conflict("Team A files work in the DPA project",
                              "Team B files work in the SEC project")
        assert c and "DPA" in c and "SEC" in c

    def test_allows_matching_identifier(self, store):
        write_config(store, entities=JIRA)
        assert k.entity_conflict("work goes in the SEC project",
                                 "SEC project holds security work") is None

    def test_does_not_block_when_one_side_lists_examples(self, store):
        """The rule that protects real merges: several ids means illustration.

        A real cluster legitimately merged a member listing sales@, support@
        with one listing billing@, ops@. Disjoint sets, same underlying fact.
        """
        write_config(store, entities=EMAIL)
        a = "aliases such as sales@x.com and support@x.com appear as one attendee"
        b = "aliases like billing@x.com and ops@x.com collapse to one"
        assert k.entity_conflict(a, b) is None

    def test_does_not_block_when_one_side_names_nothing(self, store):
        """A generalisation must be able to absorb a specific fact."""
        write_config(store, entities=JIRA)
        assert k.entity_conflict("teams file work in their own project",
                                 "SecOps files work in the SEC project") is None

    def test_bad_pattern_is_skipped_not_fatal(self, store, capsys):
        write_config(store, entities=[{"name": "broken", "patterns": ["([unclosed"]}])
        assert k.entity_conflict("a", "b") is None


# --------------------------------------------------------------- clustering

class TestClustering:
    def test_single_linkage_chains_through_a_middle_member(self):
        items = [("a", "gitops deploys reach the cluster"),
                 ("b", "gitops deploys reach the cluster from the repo"),
                 ("c", "deploys reach the cluster from the repo automatically")]
        groups = k.cluster_texts(items, 0.30)
        assert any(len(g) == 3 for g in groups), "single linkage must chain a-b-c"

    def test_threshold_separates_unrelated_items(self):
        items = [("a", "gitops deploys reach the cluster"),
                 ("b", "the coffee machine sits on floor three")]
        assert all(len(g) == 1 for g in k.cluster_texts(items, 0.30))

    def test_entity_conflict_suppresses_an_edge(self, store):
        write_config(store, entities=JIRA)
        items = [("a", "The Data Platform team files its work in the DPA project"),
                 ("b", "The SecOps team files its work in the SEC project")]
        assert all(len(g) == 1 for g in k.cluster_texts(items, 0.20)), \
            "identifier conflict must veto the edge even at a very low threshold"

    def test_single_item_is_its_own_group(self):
        assert k.cluster_texts([("only", "one thing")], 0.3) == [[0]]


# ------------------------------------------------------------------- config

class TestConfig:
    def test_env_beats_discovered_config(self, tmp_path, monkeypatch):
        other = tmp_path / "elsewhere"
        (other / "pending").mkdir(parents=True)
        (tmp_path / ".knowledge.yml").write_text("store: ./from-file\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("KNOWLEDGE_DIR", str(other))
        k._STORE_OVERRIDE = None
        k._CONFIG = None
        assert k.knowledge_dir() == other

    def test_store_override_beats_env(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        for d in (a, b):
            (d / "pending").mkdir(parents=True)
        monkeypatch.setenv("KNOWLEDGE_DIR", str(a))
        k._STORE_OVERRIDE = b
        k._CONFIG = None
        assert k.knowledge_dir() == b
        k._STORE_OVERRIDE = None

    def test_relative_store_resolves_against_config_file(self, tmp_path, monkeypatch):
        (tmp_path / ".knowledge.yml").write_text("store: ./facts\n")
        (tmp_path / "facts").mkdir()          # a missing store is fatal now
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KNOWLEDGE_DIR", raising=False)
        k._STORE_OVERRIDE = None
        k._CONFIG = None
        assert k.knowledge_dir() == tmp_path / "facts"

    def test_config_found_by_walking_up(self, tmp_path, monkeypatch):
        deep = tmp_path / "one" / "two" / "three"
        deep.mkdir(parents=True)
        (tmp_path / ".knowledge.yml").write_text("store: ./facts\n")
        monkeypatch.chdir(deep)
        assert k.find_config_file() == tmp_path / ".knowledge.yml"

    def test_malformed_config_is_fatal_not_silent(self, tmp_path, monkeypatch):
        """It used to fall back to defaults, silently relocating the store."""
        (tmp_path / ".knowledge.yml").write_text("entities:\n  - name: x\n    patterns:\n      - 'oops\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KNOWLEDGE_DIR", raising=False)
        k._STORE_OVERRIDE = None
        k._CONFIG = None
        with pytest.raises(k.typer.Exit):
            k.load_config(reload=True)

    def test_non_mapping_config_is_fatal(self, tmp_path, monkeypatch):
        (tmp_path / ".knowledge.yml").write_text("- just\n- a\n- list\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KNOWLEDGE_DIR", raising=False)
        k._STORE_OVERRIDE = None
        k._CONFIG = None
        with pytest.raises(k.typer.Exit):
            k.load_config(reload=True)

    def test_thresholds_merge_rather_than_replace(self, store):
        write_config(store, thresholds={"cluster_cosine": 0.11})
        assert k.thresh("cluster_cosine") == 0.11
        assert k.thresh("duplicate_cosine") == k.DEFAULTS["thresholds"]["duplicate_cosine"]


class TestScopes:
    def test_configured_literal_scope_accepted(self, store):
        write_config(store, scopes=["org"], prefixed_scopes=["repo"])
        k.validate_scope("org")

    def test_unconfigured_scope_rejected(self, store):
        write_config(store, scopes=["org"], prefixed_scopes=["repo"])
        with pytest.raises(Exception):
            k.validate_scope("acme-org")

    def test_prefixed_scope_requires_a_value(self, store):
        write_config(store, scopes=["org"], prefixed_scopes=["repo"])
        k.validate_scope("repo:widgets")
        with pytest.raises(Exception):
            k.validate_scope("repo:")

    def test_default_scope_is_first_literal(self, store):
        write_config(store, scopes=["team", "personal"])
        assert k.default_scope() == "team"


# ------------------------------------------------------------- duplicate gate

class TestDuplicateGate:
    def test_paraphrase_of_stored_fact_is_refused(self, store):
        write_config(store, entities=[], thresholds={"duplicate_cosine": 0.4})
        add_fact(store, "gitops", "Deployments reach clusters through GitOps, never by hand.")
        hit = k.find_near_duplicate("Deployments reach clusters via GitOps and never by hand.")
        assert hit and hit[1] == "gitops"

    def test_unrelated_fact_is_not_refused(self, store):
        write_config(store, entities=[])
        add_fact(store, "gitops", "Deployments reach clusters through GitOps, never by hand.")
        assert k.find_near_duplicate("The coffee machine is on floor three.") is None

    def test_entity_conflict_prevents_a_false_refusal(self, store):
        """The destructive failure: a genuinely new fact silently dropped."""
        write_config(store, entities=JIRA, thresholds={"duplicate_cosine": 0.3})
        add_fact(store, "sec", "The SecOps team files its work in the SEC project.")
        assert k.find_near_duplicate("The Stamps team files its work in the STMP project.") is None

    def test_invalidated_facts_are_not_duplicate_sources(self, store):
        write_config(store, entities=[], thresholds={"duplicate_cosine": 0.3})
        add_fact(store, "old", "Deployments reach clusters through GitOps.",
                 invalidated_at="2026-02-02")
        assert k.find_near_duplicate("Deployments reach clusters through GitOps.") is None

    def test_queued_candidate_can_be_the_duplicate(self, store):
        write_config(store, entities=[], thresholds={"duplicate_cosine": 0.4})
        add_candidate(store, "cand", "Deployments reach clusters through GitOps, never by hand.")
        hit = k.find_near_duplicate("Deployments reach clusters via GitOps and never by hand.")
        assert hit and hit[0] == "pending"

    def test_related_records_neighbours_without_refusing(self, store):
        write_config(store, entities=[],
                     thresholds={"duplicate_cosine": 0.95, "related_cosine": 0.2})
        add_fact(store, "gitops", "Deployments reach clusters through GitOps, never by hand.")
        near = k.find_related("Deployments reach clusters via GitOps and never by hand.")
        assert any("gitops" in n for n in near)


# ----------------------------------------------------------------- id + store

class TestIdentifiers:
    def test_derived_ids_collide_when_bodies_open_alike(self):
        """Why merges pass --id explicitly."""
        a = k.derive_id("topic", "Google Calendar returns all-day events oddly", None)
        b = k.derive_id("topic", "Google Calendar returns all-day events as timestamps", None)
        assert a == b, "documents the collision that explicit ids work around"

    def test_explicit_id_wins(self):
        assert k.derive_id("topic", "some body text", "chosen-id") == "chosen-id"


class TestFactIO:
    def test_all_facts_skips_readme(self, store):
        write_config(store)
        (store / "README.md").write_text("# not a fact\n")
        add_fact(store, "real", "A real fact.")
        assert [fid for fid, _ in k.all_facts()] == ["real"]

    def test_pending_is_not_indexed_as_facts(self, store):
        """The safety property: unvetted output must stay out of the fact set."""
        write_config(store)
        add_candidate(store, "cand", "An unvetted claim.")
        assert k.all_facts() == []
        assert [pid for pid, _ in k.all_pending()] == ["cand"]

    def test_rewrite_preserves_frontmatter_and_id(self, store):
        write_config(store)
        add_fact(store, "f", "Old body.", confidence="medium")
        import frontmatter
        post = frontmatter.load(store / "f.md")
        post.content = "\nNew body.\n"
        (store / "f.md").write_text(frontmatter.dumps(post) + "\n")
        again = frontmatter.load(store / "f.md")
        assert again.get("confidence") == "medium"
        assert again.get("id") == "f"
        assert "New body." in again.content


# ----------------------------------------------------------------------- tune

class TestTune:
    def test_rejection_reasons_group_into_labelled_clusters(self, store):
        """`tune`'s input: a rejection naming its survivor is a labelled pair."""
        write_config(store)
        rej = store / "pending" / "rejected"
        rej.mkdir(parents=True)
        for i, text in enumerate(["Results spill to a file on disk instead of inline.",
                                  "Oversized results are written to disk, not returned inline."]):
            (rej / f"c{i}.yml").write_text(yaml.safe_dump(
                {"id": f"c{i}", "text": text, "topic": "t", "scope": "org",
                 "rejection_reason": "duplicate of survivor-fact"}, sort_keys=False))
        groups: dict[str, list[str]] = {}
        import re
        for rid, data in k.all_rejected():
            m = re.search(r"duplicate of ([a-z0-9][a-z0-9._-]+)",
                          str(data.get("rejection_reason") or ""), re.I)
            if m:
                groups.setdefault(m.group(1), []).append(rid)
        assert groups == {"survivor-fact": ["c0", "c1"]}

    def _seed_review_history(self, store, clusters):
        """clusters: {survivor: [texts]} written into the rejected log."""
        rej = store / "pending" / "rejected"
        rej.mkdir(parents=True, exist_ok=True)
        n = 0
        for survivor, texts in clusters.items():
            for t in texts:
                (rej / f"r{n}.yml").write_text(yaml.safe_dump(
                    {"id": f"r{n}", "text": t, "topic": "t", "scope": "org",
                     "rejection_reason": f"duplicate of {survivor}"}, sort_keys=False))
                n += 1

    def test_exits_when_there_is_no_labelled_history(self, store):
        from typer.testing import CliRunner
        write_config(store)
        res = CliRunner().invoke(k.app, ["tune"])
        assert res.exit_code == 1
        assert "labelled data" in res.output

    def test_recommends_and_explains_the_method(self, store):
        from typer.testing import CliRunner
        write_config(store)
        self._seed_review_history(store, {
            "spill": ["Oversized results spill to a file on disk instead of inline.",
                      "Large results are written to disk rather than returned inline.",
                      "Results too big for one response land on disk, not inline."],
            "leave": ["Leave appears on the calendar as all-day vacation entries.",
                      "Vacation shows up on calendars as all-day entries.",
                      "Time off is written to the calendar as all-day entries."],
        })
        add_fact(store, "unrelated-a", "The coffee machine sits on floor three.")
        add_fact(store, "unrelated-b", "Parking badges are issued by facilities.")
        res = CliRunner().invoke(k.app, ["tune"])
        assert res.exit_code == 0, res.output
        assert "Recommendation" in res.output
        assert "cluster_cosine" in res.output
        # the point of the exercise: it teaches the method, not just a number
        assert "How this was measured" in res.output
        assert "single linkage" in res.output

    def test_reports_held_out_miss_rate_not_just_in_sample(self, store):
        from typer.testing import CliRunner
        write_config(store)
        self._seed_review_history(store, {
            "gitops-deploys": ["Deploys reach clusters through gitops, never by hand.",
                               "Deployments arrive in clusters via gitops and never manually."],
            "calendar-readable": ["Calendars are readable across the whole organisation.",
                                  "Every colleague calendar can be read org-wide."],
            "one-team-tag": ["Monitors should carry exactly one owning team tag.",
                             "A monitor carries a single team tag, never several."],
        })
        res = CliRunner().invoke(k.app, ["tune", "--folds", "3"])
        assert res.exit_code == 0, res.output
        assert "Cross-validation" in res.output
        assert "held out" in res.output

    def test_invalidated_facts_are_excluded_from_negatives(self, store):
        from typer.testing import CliRunner
        write_config(store)
        self._seed_review_history(store, {
            "gitops-deploys": ["Deploys reach clusters through gitops, never by hand.",
                               "Deployments arrive in clusters via gitops and never manually."]})
        add_fact(store, "live", "The coffee machine sits on floor three.")
        add_fact(store, "dead", "Parking badges are issued by facilities.",
                 invalidated_at="2026-02-02")
        res = CliRunner().invoke(k.app, ["tune"])
        assert res.exit_code == 0, res.output
        assert "from 1 active facts" in plain(res.output)

    def test_write_requires_a_config_file(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        s = tmp_path / "store"
        (s / "pending" / "rejected").mkdir(parents=True)
        monkeypatch.setenv("KNOWLEDGE_DIR", str(s))
        monkeypatch.chdir(tmp_path)
        k._STORE_OVERRIDE = None; k._CONFIG = None; k.load_config(reload=True)
        self._seed_review_history(s, {
            "gitops-deploys": ["Deploys reach clusters through gitops, never by hand.",
                               "Deployments arrive in clusters via gitops and never manually."]})
        res = CliRunner().invoke(k.app, ["tune", "--write"])
        assert res.exit_code == 2

    def test_write_updates_thresholds_in_place(self, store):
        from typer.testing import CliRunner
        cfg = write_config(store, thresholds={"cluster_cosine": 0.99})
        self._seed_review_history(store, {
            "gitops-deploys": ["Deploys reach clusters through gitops, never by hand.",
                               "Deployments arrive in clusters via gitops and never manually."]})
        res = CliRunner().invoke(k.app, ["tune", "--write"])
        assert res.exit_code == 0, res.output
        after = yaml.safe_load(cfg.read_text())
        assert after["thresholds"]["cluster_cosine"] != 0.99
        assert after["thresholds"]["duplicate_cosine"] > after["thresholds"]["cluster_cosine"], \
            "refusal must stay above grouping"

    def test_best_edge_is_what_single_linkage_needs(self):
        """Averaging pairs understates recall; the max edge is the real statistic."""
        texts = ["results spill to a file on disk",
                 "oversized results are written to disk",
                 "results land on disk rather than inline"]
        idf = k._idf([k.content_tokens(t) for t in texts])
        vecs = [k._vec(k.content_tokens(t), idf) for t in texts]
        best = [max(k._cos(vecs[i], vecs[j]) for j in range(3) if j != i) for i in range(3)]
        avg = [sum(k._cos(vecs[i], vecs[j]) for j in range(3) if j != i) / 2 for i in range(3)]
        assert all(b >= a for b, a in zip(best, avg))


# ---------------------------------------------------------------------- search

class TestSearch:
    def test_ranks_the_relevant_fact_first(self, store):
        from typer.testing import CliRunner
        write_config(store)
        add_fact(store, "clusters", "The platform team owns Kubernetes clusters and Terraform.")
        add_fact(store, "coffee", "The coffee machine sits on floor three.")
        res = CliRunner().invoke(k.app, ["search", "who owns kubernetes", "--json"])
        assert res.exit_code == 0, res.output
        hits = json.loads(res.output)
        assert hits[0]["id"] == "clusters"

    def test_scales_with_the_query_not_the_store(self, store):
        """The whole point: output size is bounded by --limit, not by fact count."""
        from typer.testing import CliRunner
        write_config(store)
        for i in range(40):
            add_fact(store, f"f{i}", f"Some fact number {i} about deployments and clusters.")
        res = CliRunner().invoke(k.app, ["search", "deployments", "--limit", "3", "--json"])
        assert len(json.loads(res.output)) == 3

    def test_excludes_invalidated_facts(self, store):
        from typer.testing import CliRunner
        write_config(store)
        add_fact(store, "dead", "The platform team owns Kubernetes clusters.",
                 invalidated_at="2026-02-02")
        res = CliRunner().invoke(k.app, ["search", "kubernetes", "--json"])
        assert json.loads(res.output) == []

    def test_pending_included_only_when_asked_and_labelled(self, store):
        from typer.testing import CliRunner
        write_config(store)
        add_candidate(store, "cand", "The platform team owns Kubernetes clusters.")
        plain = json.loads(CliRunner().invoke(k.app, ["search", "kubernetes", "--json"]).output)
        assert plain == []
        withp = json.loads(CliRunner().invoke(
            k.app, ["search", "kubernetes", "--pending", "--json"]).output)
        assert withp and withp[0]["kind"] == "pending"

    def test_no_match_is_not_an_error(self, store):
        from typer.testing import CliRunner
        write_config(store)
        add_fact(store, "coffee", "The coffee machine sits on floor three.")
        res = CliRunner().invoke(k.app, ["search", "kubernetes"])
        assert res.exit_code == 0


class TestIndexGating:
    def test_bare_index_refuses(self, store):
        """It used to dump every fact; that grows without bound."""
        from typer.testing import CliRunner
        write_config(store)
        add_fact(store, "f", "A fact.")
        res = CliRunner().invoke(k.app, ["index"])
        assert res.exit_code == 2
        assert "search" in res.output

    def test_topic_scoped_index_works(self, store):
        from typer.testing import CliRunner
        write_config(store)
        add_fact(store, "a", "First fact.", topic="alpha")
        add_fact(store, "b", "Second fact.", topic="beta")
        res = CliRunner().invoke(k.app, ["index", "--topic", "alpha"])
        assert res.exit_code == 0
        assert "a:" in res.output and "b:" not in res.output

    def test_all_still_dumps_everything(self, store):
        from typer.testing import CliRunner
        write_config(store)
        add_fact(store, "a", "First fact.", topic="alpha")
        add_fact(store, "b", "Second fact.", topic="beta")
        res = CliRunner().invoke(k.app, ["index", "--all"])
        assert "a:" in res.output and "b:" in res.output


# ----------------------------------------------------------------- merge flow

class TestMergeProposals:
    def test_merge_is_exempt_from_the_duplicate_gate(self, store):
        """A consolidation resembles its sources by construction."""
        from typer.testing import CliRunner
        write_config(store, entities=[], thresholds={"duplicate_cosine": 0.3})
        add_fact(store, "src-one", "Deployments reach clusters through GitOps, never by hand.")
        res = CliRunner().invoke(k.app, [
            "propose", "Deployments reach clusters through GitOps and never by hand.",
            "--topic", "t", "--merges", "src-one"])
        assert res.exit_code == 0, res.output
        assert "Queued" in res.output

    def test_without_merges_the_same_text_is_refused(self, store):
        from typer.testing import CliRunner
        write_config(store, entities=[], thresholds={"duplicate_cosine": 0.3})
        add_fact(store, "src-one", "Deployments reach clusters through GitOps, never by hand.")
        res = CliRunner().invoke(k.app, [
            "propose", "Deployments reach clusters through GitOps and never by hand.",
            "--topic", "t"])
        assert "Skipped" in res.output

    def test_merges_must_name_existing_facts(self, store):
        from typer.testing import CliRunner
        write_config(store)
        res = CliRunner().invoke(k.app, ["propose", "Anything.", "--topic", "t",
                                         "--merges", "does-not-exist"])
        assert res.exit_code == 2

    def test_merge_ids_are_recorded_on_the_candidate(self, store):
        from typer.testing import CliRunner
        write_config(store)
        add_fact(store, "src-one", "One.")
        add_fact(store, "src-two", "Two.")
        CliRunner().invoke(k.app, ["propose", "One and two together.", "--topic", "t",
                                   "--id", "merged", "--merges", "src-one,src-two"])
        rec = yaml.safe_load((store / "pending" / "merged.yml").read_text())
        assert rec["merges"] == ["src-one", "src-two"]

    def test_confirm_retires_the_merged_sources(self, store):
        from typer.testing import CliRunner
        import frontmatter
        write_config(store)
        add_fact(store, "src-one", "One.")
        add_fact(store, "src-two", "Two.")
        CliRunner().invoke(k.app, ["propose", "One and two together.", "--topic", "t",
                                   "--id", "merged", "--merges", "src-one,src-two"])
        res = CliRunner().invoke(k.app, ["confirm", "merged"])
        assert res.exit_code == 0, res.output
        for src in ("src-one", "src-two"):
            post = frontmatter.load(store / f"{src}.md")
            assert post.get("invalidated_at"), f"{src} should be retired"
            assert "merged" in str(post.get("invalidation_reason"))

    def test_confirm_without_merges_retires_nothing(self, store):
        from typer.testing import CliRunner
        import frontmatter
        write_config(store)
        add_fact(store, "untouched", "Unrelated fact.")
        add_candidate(store, "plain", "A plain candidate.")
        CliRunner().invoke(k.app, ["confirm", "plain"])
        assert frontmatter.load(store / "untouched.md").get("invalidated_at") is None


# ------------------------------------------------------------------- recall

class TestRecall:
    """Recall runs unattended on every prompt, so its job is to stay silent
    unless it is confident. These are the cases that made it fire wrongly."""

    def _seed(self, store):
        add_fact(store, "gitops-deploys",
                 "Deploys reach clusters through argocd, never by hand.", topic="deploy")
        add_fact(store, "freight-promotion",
                 "Kargo promotes freight to a stamp on image push.", topic="deploy")
        add_fact(store, "token-storage",
                 "The incident tool api token lives in the password manager.", topic="secrets")

    def _hits(self, store, query, **kw):
        from typer.testing import CliRunner
        args = ["recall", query, "--json"]
        for key, val in kw.items():
            args += [f"--{key.replace('_', '-')}", str(val)]
        res = CliRunner().invoke(k.app, args)
        assert res.exit_code == 0, res.output
        return json.loads(res.stdout)["hits"]

    def test_generic_vocabulary_alone_does_not_fire(self, store):
        """The bug this gate exists for: a conversational follow-up matched two
        generic tokens and injected three unrelated facts."""
        self._seed(store)
        assert self._hits(store, "did you do the config file thing too?") == []

    def test_distinctive_vocabulary_fires(self, store):
        self._seed(store)
        ids = [h["id"] for h in self._hits(store, "how does kargo promote freight to a stamp")]
        assert "freight-promotion" in ids

    def test_pending_bodies_are_never_returned(self, store):
        """Unvetted text must not reach a prompt unattended, whatever it scores."""
        self._seed(store)
        add_candidate(store, "kargo-guess", "Kargo promotes freight to a stamp on merge.")
        hits = self._hits(store, "how does kargo promote freight to a stamp")
        assert all(h["id"] != "kargo-guess" for h in hits)

    def test_stale_facts_surface_with_their_marker(self, store):
        add_fact(store, "old-freight", "Kargo promotes freight to a stamp on image push.",
                 topic="deploy")
        path = store / "old-freight.md"
        path.write_text(path.read_text().replace("last_verified_at: '2026-01-01'",
                                                 "last_verified_at: '2000-01-01'"))
        hits = self._hits(store, "how does kargo promote freight to a stamp")
        assert hits and hits[0]["status"].startswith("STALE")

    def test_empty_store_is_silent_not_an_error(self, store):
        assert self._hits(store, "how does kargo promote freight to a stamp") == []


# ----------------------------------------------------------------- evidence

class TestEvidence:
    def test_confirm_carries_evidence_into_the_stored_fact(self, store):
        """It used to be dropped at promotion, leaving `source: session-<date>`
        as the only pointer — unverifiable without redoing the research."""
        from typer.testing import CliRunner
        add_candidate(store, "t-claim", "Deploys reach clusters through argocd.",
                      scope="personal", evidence="alice in #eng: 'argocd, never by hand'")
        res = CliRunner().invoke(k.app, ["confirm", "t-claim"])
        assert res.exit_code == 0, res.output
        assert "argocd, never by hand" in (store / "t-claim.md").read_text()


# ------------------------------------------------------------------ pruning

class TestPruneArchives:
    def test_unreviewed_candidate_is_archived_not_deleted(self, store):
        """Aging out means review didn't keep up, not that the claim was wrong."""
        from typer.testing import CliRunner
        add_candidate(store, "old-one", "Something nobody ever looked at.")
        res = CliRunner().invoke(k.app, ["prune-pending", "--days", "1"])
        assert res.exit_code == 0, res.output
        assert not (store / "pending" / "old-one.yml").exists()
        archived = store / "pending" / "archived" / "old-one.yml"
        assert archived.exists()
        assert "unreviewed for" in yaml.safe_load(archived.read_text())["archived_reason"]

    def test_archived_candidates_can_be_re_proposed(self, store):
        """Out of the duplicate corpus on purpose: if it matters again, saying so
        again is the signal that it does."""
        from typer.testing import CliRunner
        add_candidate(store, "old-one", "Deploys reach clusters through argocd, never by hand.",
                      scope="personal")
        CliRunner().invoke(k.app, ["prune-pending", "--days", "1"])
        res = CliRunner().invoke(k.app, [
            "propose", "Deploys reach clusters through argocd, never by hand.",
            "--topic", "deploy", "--scope", "personal"])
        assert res.exit_code == 0, res.output
        assert "Skipped" not in res.output


# -------------------------------------------------------------------- usage

class TestUsage:
    def test_get_logs_a_read(self, store):
        from typer.testing import CliRunner
        add_fact(store, "a-fact", "Deploys reach clusters through argocd.")
        CliRunner().invoke(k.app, ["get", "a-fact"])
        rows = [json.loads(x) for x in
                (store / k.USAGE_LOG_NAME).read_text().splitlines() if x.strip()]
        assert any(r["action"] == "get" and "a-fact" in r["ids"] for r in rows)

    def test_recall_misses_are_logged_for_tuning(self, store):
        """The miss log is the feedback loop for the score floor; without it,
        retuning is guesswork."""
        from typer.testing import CliRunner
        add_fact(store, "a-fact", "Deploys reach clusters through argocd.")
        CliRunner().invoke(k.app, ["recall", "something entirely unrelated to anything", "--quiet"])
        rows = [json.loads(x) for x in
                (store / k.USAGE_LOG_NAME).read_text().splitlines() if x.strip()]
        assert any(r["action"] == "recall" and r["hit"] is False for r in rows)

    def test_logging_can_be_disabled(self, store, monkeypatch):
        from typer.testing import CliRunner
        monkeypatch.setenv("KNOWLEDGE_USAGE_LOG", "0")
        add_fact(store, "a-fact", "Deploys reach clusters through argocd.")
        CliRunner().invoke(k.app, ["get", "a-fact"])
        assert not (store / k.USAGE_LOG_NAME).exists()


class TestStoreOwnsItsSettings:
    """A config that only NAMES a store must inherit that store's settings.

    Before this, a pointer config — the user-level fallback, or one at the root
    of a source tree — merged alone. Scope vocabulary, thresholds and the entity
    guard all silently fell back to defaults, so `--scope <yours>` started being
    rejected and the guard went inert with nothing said. That is the same silent
    degradation a malformed config is made fatal to prevent.
    """

    def _store_with_settings(self, tmp_path):
        s = tmp_path / "thestore"
        (s / "pending").mkdir(parents=True)
        (s / ".knowledge.yml").write_text(yaml.safe_dump({
            "store": ".",
            "scopes": ["acme-org", "personal"],
            "prefixed_scopes": ["repo"],
            "thresholds": {"cluster_cosine": 0.11},
            "entities": [{"name": "ticket", "patterns": [r"\b([A-Z]{2,6})-\d+\b"]}],
        }, sort_keys=False))
        return s

    def _pointer(self, tmp_path, store, monkeypatch):
        """A config elsewhere that only names the store."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        ptr = elsewhere / ".knowledge.yml"
        ptr.write_text(f"store: {store}\n")
        monkeypatch.chdir(elsewhere)
        monkeypatch.delenv("KNOWLEDGE_DIR", raising=False)
        k._STORE_OVERRIDE = None
        k.load_config(reload=True)
        return ptr

    def test_scope_vocabulary_comes_from_the_store(self, tmp_path, monkeypatch):
        s = self._store_with_settings(tmp_path)
        self._pointer(tmp_path, s, monkeypatch)
        k.validate_scope("acme-org")          # would have raised

    def test_thresholds_come_from_the_store(self, tmp_path, monkeypatch):
        s = self._store_with_settings(tmp_path)
        self._pointer(tmp_path, s, monkeypatch)
        assert k.thresh("cluster_cosine") == 0.11

    def test_entity_guard_stays_armed(self, tmp_path, monkeypatch):
        s = self._store_with_settings(tmp_path)
        self._pointer(tmp_path, s, monkeypatch)
        assert k.entity_conflict("filed under ABC-1", "filed under XYZ-2")

    def test_the_pointer_still_overrides_what_it_sets(self, tmp_path, monkeypatch):
        """Inheriting the store's settings must not make them unoverridable."""
        s = self._store_with_settings(tmp_path)
        ptr = self._pointer(tmp_path, s, monkeypatch)
        ptr.write_text(f"store: {s}\nthresholds:\n  cluster_cosine: 0.99\n")
        k.load_config(reload=True)
        assert k.thresh("cluster_cosine") == 0.99
        k.validate_scope("acme-org")          # unset keys still inherited


class TestOnlyFactsAreFacts:
    def test_prose_in_the_store_is_not_a_fact(self, store):
        """`init --store .` writes AGENTS-knowledge.md into the store, and a
        glob of *.md counted it — a brand-new empty store reported 1 fact."""
        (store / "AGENTS-knowledge.md").write_text("# protocol\n\nPaste into AGENTS.md.\n")
        (store / "README.md").write_text("# my store\n")
        assert k.all_facts() == []

    def test_real_facts_still_load(self, store):
        add_fact(store, "real-one", "A stored claim.")
        (store / "AGENTS-knowledge.md").write_text("# protocol\n")
        assert [fid for fid, _ in k.all_facts()] == ["real-one"]


# ------------------------------------------------------ episodes / graph v2

class TestEpisodeIngestion:
    def test_ingest_is_append_only_and_does_not_dedupe(self, store):
        from typer.testing import CliRunner
        runner = CliRunner()
        for _ in range(2):
            res = runner.invoke(k.app, ["ingest", "The same observation.", "--quiet"])
            assert res.exit_code == 0, res.output
        episodes = k.all_episodes()
        assert len(episodes) == 2
        assert episodes[0][0] != episodes[1][0]
        assert all(data["trusted"] is False for _, data in episodes)

    def test_propose_creates_and_links_an_episode(self, store):
        from typer.testing import CliRunner
        res = CliRunner().invoke(k.app, [
            "propose", "Deployments use GitOps.", "--topic", "deploy",
            "--evidence", "config/deploy.yaml:12",
        ])
        assert res.exit_code == 0, res.output
        candidate = yaml.safe_load(next((store / "pending").glob("*.yml")).read_text())
        assert len(candidate["episodes"]) == 1
        assert {eid for eid, _ in k.all_episodes()} == set(candidate["episodes"])

    def test_episode_get_is_clearly_untrusted(self, store):
        from typer.testing import CliRunner
        eid = CliRunner().invoke(k.app, ["ingest", "Raw source.", "--quiet"]).stdout.strip()
        res = CliRunner().invoke(k.app, ["get", eid])
        assert "UNTRUSTED EPISODE" in res.stdout
        assert "Raw source" in res.stdout

    def test_migrate_backfills_legacy_assertions_idempotently(self, store):
        from typer.testing import CliRunner
        add_fact(store, "legacy", "A legacy confirmed assertion.", learned_at="2025-01-01",
                 evidence="old-doc.md:10")
        add_candidate(store, "legacy-candidate", "A legacy candidate.",
                      evidence="old-doc.md:20", proposed_at="2025-02-01")
        runner = CliRunner()
        first = runner.invoke(k.app, ["migrate", "--no-rebuild-graph", "--json"])
        assert first.exit_code == 0, first.output
        assert json.loads(first.stdout) == {
            "facts_changed": 1, "pending_changed": 1, "episodes_created": 2, "episodes_updated": 0,
            "dry_run": False, "graph": {},
        }
        post = frontmatter.load(store / "legacy.md")
        candidate = yaml.safe_load((store / "pending" / "legacy-candidate.yml").read_text())
        assert post["epistemic_status"] == "supported"
        assert post["recorded_at"]
        episode = dict(k.all_episodes())[post["episodes"][0]]
        assert episode["text"] == "old-doc.md:10"
        assert candidate["recorded_at"] == "2025-02-01"
        assert len(post["episodes"]) == len(candidate["episodes"]) == 1
        assert len(k.all_episodes()) == 2

        second = runner.invoke(k.app, ["migrate", "--no-rebuild-graph", "--json"])
        assert second.exit_code == 0, second.output
        assert json.loads(second.stdout)["facts_changed"] == 0
        assert json.loads(second.stdout)["pending_changed"] == 0
        assert json.loads(second.stdout)["episodes_created"] == 0

    def test_migrate_uses_assertion_text_when_legacy_evidence_is_absent(self, store):
        from typer.testing import CliRunner
        add_fact(store, "no-evidence", "The legacy assertion body.", source="session-2025-01-01")
        res = CliRunner().invoke(k.app, ["migrate", "--no-pending", "--no-rebuild-graph", "--json"])
        assert res.exit_code == 0, res.output
        post = frontmatter.load(store / "no-evidence.md")
        episode = dict(k.all_episodes())[post["episodes"][0]]
        assert episode["text"] == "The legacy assertion body."
        assert episode["source"] == "session-2025-01-01"


class TestTypedAssertionsAndGraph:
    def test_confirm_preserves_typed_temporal_and_repo_fields(self, store):
        from typer.testing import CliRunner
        runner = CliRunner()
        res = runner.invoke(k.app, [
            "propose", "Widgets owns checkout.", "--topic", "ownership",
            "--subject", "service:checkout", "--predicate", "owned_by",
            "--object", "team:widgets", "--valid-from", "2026-01-01",
            "--valid-to", "2027-01-01", "--repo", "acme/checkout",
            "--id", "checkout-owner",
        ])
        assert res.exit_code == 0, res.output
        res = runner.invoke(k.app, ["confirm", "checkout-owner"])
        assert res.exit_code == 0, res.output
        post = frontmatter.load(store / "checkout-owner.md")
        assert post["subject"] == "service:checkout"
        assert post["predicate"] == "owned_by"
        assert post["object"] == "team:widgets"
        assert post["valid_from"] == "2026-01-01"
        assert post["valid_to"] == "2027-01-01"
        assert post["repo"] == "acme/checkout"
        assert post["epistemic_status"] == "supported"

    def test_graph_projection_is_external_and_contains_edges(self, store):
        from typer.testing import CliRunner
        cache = store.parent / "cache"
        write_config(store, graph_cache=str(cache))
        add_fact(
            store, "owner", "Widgets owns checkout.",
            subject="service:checkout", predicate="owned_by", object="team:widgets",
            epistemic_status="supported",
        )
        res = CliRunner().invoke(k.app, ["graph-rebuild", "--json"])
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        graph = Path(payload["path"])
        assert graph.is_file()
        assert store not in graph.parents
        assert payload["entities"] == 2
        assert payload["edges"] == 1

        conn = k.sqlite3.connect(graph)
        assert conn.execute("SELECT subject,predicate,object FROM edges").fetchone() == (
            "service:checkout", "owned_by", "team:widgets")
        conn.close()

    def test_corrupt_projection_is_rebuilt_without_losing_assertions(self, store):
        write_config(store, graph_cache=str(store.parent / "cache"))
        add_fact(store, "owner", "Widgets owns checkout.", subject="service:checkout",
                 predicate="owned_by", object="team:widgets")
        graph = k.graph_index_path()
        graph.parent.mkdir(parents=True, exist_ok=True)
        graph.write_text("not a sqlite database")
        rebuilt = k.ensure_graph()
        conn = k.sqlite3.connect(rebuilt)
        assert conn.execute("SELECT id FROM assertions").fetchone()[0] == "owner"
        conn.close()

    def test_graph_projection_binds_assertion_text_as_data(self, store):
        write_config(store, graph_cache=str(store.parent / "cache"))
        add_fact(store, "quote", "'); DROP TABLE assertions; --", subject="service:checkout",
                 predicate="owned_by", object="team:widgets")
        k.rebuild_graph()
        conn = k.sqlite3.connect(k.graph_index_path())
        assert conn.execute("SELECT text FROM assertions WHERE id='quote'").fetchone()[0] == "'); DROP TABLE assertions; --"
        assert conn.execute("SELECT count(*) FROM assertions").fetchone()[0] == 1
        conn.close()

    def test_neighbors_excludes_pending_unless_requested(self, store):
        from typer.testing import CliRunner
        write_config(store, graph_cache=str(store.parent / "cache"))
        add_fact(store, "owner", "Widgets owns checkout.", subject="service:checkout",
                 predicate="owned_by", object="team:widgets")
        add_candidate(store, "candidate", "Checkout deploys to prod.",
                      subject="service:checkout", predicate="deployed_to", object="env:prod")
        runner = CliRunner()
        plain = json.loads(runner.invoke(
            k.app, ["graph-neighbors", "service:checkout", "--json"]).stdout)
        with_pending = json.loads(runner.invoke(
            k.app, ["graph-neighbors", "service:checkout", "--pending", "--json"]).stdout)
        assert {x["id"] for x in plain} == {"owner"}
        assert {x["id"] for x in with_pending} == {"owner", "candidate"}

    def test_neighbors_excludes_invalidated_assertions(self, store):
        from typer.testing import CliRunner
        write_config(store, graph_cache=str(store.parent / "cache"))
        add_fact(store, "old-owner", "Widgets owns checkout.",
                 subject="service:checkout", predicate="owned_by", object="team:widgets",
                 invalidated_at="2026-01-02")
        rows = json.loads(CliRunner().invoke(
            k.app, ["graph-neighbors", "service:checkout", "--json"]).stdout)
        assert rows == []


class TestRepoAndDateAwareRetrieval:
    def test_retrieval_fixture_corpus_preserves_repo_and_time_isolation(self, store, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        corpus = json.loads((Path(__file__).parent / "fixtures/retrieval-corpus.json").read_text())
        for row in corpus:
            add_fact(store, row.pop("id"), row.pop("text"), **row)
        repo = tmp_path / "widgets"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        now = json.loads(CliRunner().invoke(k.app, ["search", "owns checkout", "--as-of", "2026-06-01", "--json"]).stdout)
        old = json.loads(CliRunner().invoke(k.app, ["search", "owns checkout", "--as-of", "2025-06-01", "--json"]).stdout)
        # Valid time still isolates. The cwd repo no longer does: it only
        # reorders, so the acme/other fact stays eligible from acme/widgets.
        assert {row["id"] for row in now} == {"widgets", "other"}
        assert {row["id"] for row in old} == {"old"}

    def test_cwd_repo_ranks_but_never_excludes_other_repos(self, store, monkeypatch):
        from typer.testing import CliRunner
        add_fact(store, "here", "Checkout uses GitOps.", repo="acme/checkout")
        add_fact(store, "elsewhere", "Checkout uses GitOps elsewhere.", repo="acme/other")
        add_fact(store, "global", "GitOps is the deployment convention.")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "acme/checkout")
        monkeypatch.setattr(k, "repo_checkout", lambda repo, cwd="": None)
        rows = json.loads(CliRunner().invoke(
            k.app, ["search", "gitops deployment", "--json"]).stdout)
        assert {row["id"] for row in rows} == {"here", "elsewhere", "global"}

    def test_explicit_repo_restricts_where_cwd_does_not(self, store, monkeypatch):
        from typer.testing import CliRunner
        add_fact(store, "here", "Checkout uses GitOps.", repo="acme/checkout")
        add_fact(store, "elsewhere", "Checkout uses GitOps elsewhere.", repo="acme/other")
        add_fact(store, "global", "GitOps is the deployment convention.")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "acme/checkout")
        monkeypatch.setattr(k, "repo_checkout", lambda repo, cwd="": None)
        rows = json.loads(CliRunner().invoke(
            k.app, ["search", "gitops deployment", "--repo", "acme/checkout", "--json"]).stdout)
        assert {row["id"] for row in rows} == {"here", "global"}
        widened = json.loads(CliRunner().invoke(
            k.app, ["search", "gitops deployment", "--repo", "acme/checkout",
                    "--all-repos", "--json"]).stdout)
        assert {row["id"] for row in widened} == {"here", "elsewhere", "global"}

    def test_recall_surfaces_other_repo_facts_from_inside_a_checkout(self, store, monkeypatch):
        """The regression this rule exists for: a gatekeeper fact recalled
        while standing in a different checkout. Exclusion returned nothing."""
        from typer.testing import CliRunner
        add_fact(store, "gates", "Gatekeeper requires an availability SLO before production.",
                 repo="acme/gatekeeper")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "acme/k8s")
        monkeypatch.setattr(k, "repo_checkout", lambda repo, cwd="": None)
        res = CliRunner().invoke(
            k.app, ["recall", "does gatekeeper require an availability slo", "--json"])
        assert [h["id"] for h in json.loads(res.stdout)["hits"]] == ["gates"]

    def test_recall_boosts_the_local_repo_above_an_equal_match(self, store, monkeypatch):
        from typer.testing import CliRunner
        add_fact(store, "local", "Kargo promotes freight to a stamp.", repo="acme/k8s")
        add_fact(store, "remote", "Kargo promotes freight to a stamp.", repo="acme/other")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "acme/k8s")
        monkeypatch.setattr(k, "repo_checkout", lambda repo, cwd="": None)
        res = CliRunner().invoke(
            k.app, ["recall", "how does kargo promote freight to a stamp", "--json"])
        assert [h["id"] for h in json.loads(res.stdout)["hits"]] == ["local", "remote"]

    def test_search_respects_valid_time(self, store):
        from typer.testing import CliRunner
        add_fact(store, "old", "Widgets owns checkout.", valid_from="2025-01-01",
                 valid_to="2026-01-01")
        add_fact(store, "new", "Platform owns checkout.", valid_from="2026-01-01")
        old = json.loads(CliRunner().invoke(
            k.app, ["search", "owns checkout", "--as-of", "2025-06-01", "--json"]).stdout)
        assert {row["id"] for row in old} == {"old"}
        new = json.loads(CliRunner().invoke(
            k.app, ["search", "owns checkout", "--as-of", "2026-06-01", "--json"]).stdout)
        assert {row["id"] for row in new} == {"new"}

    def test_refuted_assertions_never_recall(self, store):
        from typer.testing import CliRunner
        add_fact(store, "dead", "Kargo promotes freight.", epistemic_status="refuted")
        res = CliRunner().invoke(k.app, ["recall", "how does kargo promote freight", "--json"])
        assert json.loads(res.stdout)["hits"] == []


class TestTypedCandidateExtraction:
    def test_extract_dependency_map_and_validate_it(self, store, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        repo = TestSweeper()._git_repo(tmp_path)
        (repo / "dependencies.yaml").write_text("dependencies:\n  - acme/auth\n")
        subprocess.run(["git", "add", "dependencies.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "dependencies"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        eid, _ = k.write_episode("dependencies:\n  - acme/auth\n", source="dependencies.yaml",
                                 repo="acme/widgets", path="dependencies.yaml")
        runner = CliRunner()
        extracted = runner.invoke(k.app, ["extract", eid, "--extractor", "dependency-map", "--json"])
        assert extracted.exit_code == 0, repr(extracted.exception)
        candidate = json.loads(extracted.stdout)["candidates"][0]
        assert candidate["subject"] == "repo:acme/widgets"
        assert candidate["object"] == "repo:acme/auth"
        add_fact(store, "dependency", candidate["text"], repo="acme/widgets",
                 subject=candidate["subject"], predicate=candidate["predicate"], object=candidate["object"],
                 repo_path="dependencies.yaml", validator={"type": "git-dependency-map", "path": "dependencies.yaml"})
        assert json.loads(runner.invoke(k.app, ["sweep", "dependency", "--json"]).stdout)[0]["outcome"] == "supported"
    def test_extract_codeowners_creates_evidence_linked_pending_edges(self, store):
        from typer.testing import CliRunner

        eid, _ = k.write_episode(
            "# owners\n/services/payments/** @acme/payments\n/docs/** docs@example.com @writer\n"
            "Payments is owned by @acme/payments.",
            kind="text", source="CODEOWNERS", repo="acme/widgets", path="CODEOWNERS",
        )
        runner = CliRunner()
        res = runner.invoke(k.app, ["extract", eid, "--json"])
        assert res.exit_code == 0, repr(res.exception)
        rows = json.loads(res.stdout)["candidates"]
        assert len(rows) == 3
        assert {row["status"] for row in rows} == {"queued"}

        pending = dict(k.all_pending())
        team = next(item for item in pending.values()
                    if item["object"] == "github-team:acme/payments")
        assert team["subject"] == "repo-path:acme/widgets:/services/payments/**"
        assert team["predicate"] == "owned_by"
        assert team["episodes"] == [eid]
        assert "line 2: /services/payments/** @acme/payments" in team["evidence"]
        assert team["provenance"] == "inferred"
        assert team["confidence"] == "low"
        assert k.all_facts() == []

        graph = runner.invoke(
            k.app,
            ["graph-neighbors", team["subject"], "--pending", "--json"],
        )
        assert graph.exit_code == 0, repr(graph.exception)
        assert json.loads(graph.stdout)[0]["object"] == "github-team:acme/payments"

    def test_extract_is_idempotent_and_rejects_prose(self, store):
        from typer.testing import CliRunner

        eid, _ = k.write_episode(
            "/services/** @acme/platform\nPlatform owns the services directory.",
            source="CODEOWNERS", repo="acme/widgets", path="CODEOWNERS",
        )
        runner = CliRunner()
        first = runner.invoke(k.app, ["extract", eid, "--json"])
        second = runner.invoke(k.app, ["extract", eid, "--json"])
        assert first.exit_code == second.exit_code == 0
        assert len(json.loads(first.stdout)["candidates"]) == 1
        assert json.loads(second.stdout)["candidates"][0]["status"] == "already_pending"
        assert len(k.all_pending()) == 1

    def test_extract_requires_a_repo_on_the_episode(self, store):
        from typer.testing import CliRunner

        eid, _ = k.write_episode("/services/** @acme/platform", source="CODEOWNERS")
        res = CliRunner().invoke(k.app, ["extract", eid, "--json"])
        assert res.exit_code != 0


class TestSweeper:
    def _git_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "checkout"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=repo, check=True)
        (repo / "deploy.yaml").write_text("mode: gitops\n")
        subprocess.run(["git", "add", "deploy.yaml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
        return repo

    def test_sweep_supports_then_refutes_against_git(self, store, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        repo = self._git_repo(tmp_path)
        monkeypatch.chdir(repo)
        add_fact(
            store, "deploy-file", "The repository contains deploy.yaml.",
            repo="acme/widgets", subject="repo:acme/widgets", predicate="contains_path",
            object="path:deploy.yaml", repo_path="deploy.yaml",
            validator={"type": "git-path", "path": "deploy.yaml"},
            epistemic_status="unknown",
        )
        runner = CliRunner()
        res = runner.invoke(k.app, ["sweep", "deploy-file", "--json"])
        assert res.exit_code == 0, repr(res.exception)
        assert json.loads(res.stdout)[0]["outcome"] == "supported"
        assert frontmatter.load(store / "deploy-file.md")["epistemic_status"] == "supported"

        (repo / "deploy.yaml").unlink()
        subprocess.run(["git", "add", "-u"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "remove deploy file"], cwd=repo, check=True)
        res = runner.invoke(k.app, ["sweep", "deploy-file", "--json"])
        assert res.exit_code == 0, repr(res.exception)
        assert json.loads(res.stdout)[0]["outcome"] == "contradicted"
        post = frontmatter.load(store / "deploy-file.md")
        assert post["epistemic_status"] == "refuted"
        assert len(k.read_jsonl(k.VALIDATION_LOG_NAME)) == 2

    def test_missing_checkout_becomes_unknown_not_refuted(self, store):
        from typer.testing import CliRunner
        add_fact(
            store, "missing", "The repository contains deploy.yaml.",
            repo="acme/missing", subject="repo:acme/missing", predicate="contains_path",
            object="path:deploy.yaml", repo_path="deploy.yaml",
            validator={"type": "git-path", "path": "deploy.yaml"},
        )
        res = CliRunner().invoke(k.app, ["sweep", "missing", "--json"])
        assert res.exit_code == 0, repr(res.exception)
        assert json.loads(res.stdout)[0]["outcome"] == "unknown"
        assert frontmatter.load(store / "missing.md")["epistemic_status"] == "unknown"

    def test_sweep_validates_explicit_codeowners_relation(self, store, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        repo = self._git_repo(tmp_path)
        (repo / "CODEOWNERS").write_text("/services/** @acme/platform\n")
        subprocess.run(["git", "add", "CODEOWNERS"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "add owners"], cwd=repo, check=True)
        monkeypatch.chdir(repo)
        add_fact(
            store, "service-owner", "The service path is owned by the platform team.",
            repo="acme/widgets", subject="repo-path:acme/widgets:/services/**",
            predicate="owned_by", object="github-team:acme/platform", repo_path="CODEOWNERS",
            validator={"type": "git-codeowners-owner", "path": "CODEOWNERS"},
            epistemic_status="unknown",
        )
        runner = CliRunner()
        supported = runner.invoke(k.app, ["sweep", "service-owner", "--json"])
        assert supported.exit_code == 0, repr(supported.exception)
        assert json.loads(supported.stdout)[0]["outcome"] == "supported"

        (repo / "CODEOWNERS").write_text("/services/** @acme/other\n")
        subprocess.run(["git", "add", "CODEOWNERS"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "change owners"], cwd=repo, check=True)
        contradicted = runner.invoke(k.app, ["sweep", "service-owner", "--json"])
        assert contradicted.exit_code == 0, repr(contradicted.exception)
        assert json.loads(contradicted.stdout)[0]["outcome"] == "contradicted"

    def test_sweep_explain_prioritizes_unverified_unknown_assertions(self, store):
        from typer.testing import CliRunner

        add_fact(store, "old", "Old validator-backed fact.", repo="acme/widgets",
                 subject="repo:acme/widgets", predicate="contains_path", object="path:old",
                 repo_path="old", validator={"type": "git-path", "path": "old"},
                 last_verified_at="2026-01-01", epistemic_status="supported")
        add_fact(store, "unknown", "Unknown validator-backed fact.", repo="acme/widgets",
                 subject="repo:acme/widgets", predicate="contains_path", object="path:new",
                 repo_path="new", validator={"type": "git-path", "path": "new"},
                 last_verified_at="", epistemic_status="unknown")
        res = CliRunner().invoke(k.app, ["sweep", "--explain", "--json"])
        assert res.exit_code == 0, repr(res.exception)
        selection = json.loads(res.stdout)["selection"]
        assert [row["id"] for row in selection] == ["unknown", "old"]
        assert "never validated" in selection[0]["reasons"]
        assert k.read_jsonl(k.VALIDATION_LOG_NAME) == []

    def test_sweep_records_bounded_run_summary(self, store):
        from typer.testing import CliRunner
        add_fact(store, "one", "One.", subject="repo:x/y", predicate="contains_path", object="path:one",
                 repo_path="one", validator={"type": "git-path", "path": "one"})
        add_fact(store, "two", "Two.", subject="repo:x/y", predicate="contains_path", object="path:two",
                 repo_path="two", validator={"type": "git-path", "path": "two"})
        CliRunner().invoke(k.app, ["sweep", "--limit", "1", "--run-id", "nightly"])
        summary = k.read_jsonl(k.SWEEP_LOG_NAME)[0]
        assert summary["run_id"] == "nightly"
        assert len(summary["selected"]) == len(summary["results"]) == 1


class TestAgentProvenance:
    """Who wrote this, in which session.

    A store several harnesses write to is otherwise unattributable: review sees
    a claim with no way to tell a Claude session from a Codex one, and a bad
    batch cannot be traced back to the run that produced it.
    """

    @pytest.fixture(autouse=True)
    def _no_ambient_agent(self, monkeypatch):
        """The suite itself runs inside an agent. Clear the markers so a test
        asserting "unattributed" does not pass or fail on who ran it."""
        for var in ("KNOWLEDGE_AGENT", "KNOWLEDGE_SESSION_ID", "CLAUDECODE",
                    "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                    "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_HOME",
                    "CURSOR_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)

    def test_claude_code_is_detected_from_its_session_env(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aed12345-0000")
        assert k.agent_identity() == {"agent": "claude", "session_id": "aed12345-0000"}

    def test_codex_is_detected_and_not_confused_with_claude(self, monkeypatch):
        monkeypatch.setenv("CODEX_SESSION_ID", "cx-7")
        assert k.agent_identity() == {"agent": "codex", "session_id": "cx-7"}

    def test_explicit_env_overrides_the_sniffer(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ignored")
        monkeypatch.setenv("KNOWLEDGE_AGENT", "somebot")
        monkeypatch.setenv("KNOWLEDGE_SESSION_ID", "s-1")
        assert k.agent_identity() == {"agent": "somebot", "session_id": "s-1"}

    def test_unknown_harness_is_blank_not_guessed(self):
        assert k.agent_identity() == {"agent": "", "session_id": ""}

    def test_propose_records_the_writing_agent(self, store, monkeypatch):
        from typer.testing import CliRunner
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aed99")
        CliRunner().invoke(k.app, ["propose", "Kargo promotes freight to stamps.",
                                   "--topic", "kargo", "--id", "p1", "--evidence", "e"])
        data = yaml.safe_load((store / "pending" / "p1.yml").read_text())
        assert data["agent"] == "claude" and data["session_id"] == "aed99"

    def test_confirm_keeps_the_proposing_session_not_the_confirming_one(self, store, monkeypatch):
        """Provenance is about which model run made the claim. Confirmation is a
        human act already stamped by learned_at."""
        from typer.testing import CliRunner
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "proposer")
        CliRunner().invoke(k.app, ["propose", "Kargo promotes freight to stamps.",
                                   "--topic", "kargo", "--id", "p1", "--evidence", "e"])
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "confirmer")
        CliRunner().invoke(k.app, ["confirm", "p1"])
        post = frontmatter.loads((store / "p1.md").read_text())
        assert post["agent"] == "claude" and post["session_id"] == "proposer"

    def test_ingest_records_the_writing_agent(self, store, monkeypatch):
        from typer.testing import CliRunner
        monkeypatch.setenv("CODEX_SESSION_ID", "cx-9")
        CliRunner().invoke(k.app, ["ingest", "Something observed.", "--source", "session"])
        eid, data = k.all_episodes()[0]
        assert data["agent"] == "codex" and data["session_id"] == "cx-9"

    def test_events_and_usage_carry_the_agent(self, store, monkeypatch):
        from typer.testing import CliRunner
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "aed99")
        add_fact(store, "a-fact", "Deploys reach clusters through argocd.")
        CliRunner().invoke(k.app, ["get", "a-fact"])
        CliRunner().invoke(k.app, ["propose", "Argocd runs per cluster.",
                                   "--topic", "argocd", "--id", "p2", "--evidence", "e"])
        usage = [json.loads(x) for x in
                 (store / k.USAGE_LOG_NAME).read_text().splitlines() if x.strip()]
        assert all(r["agent"] == "claude" and r["session_id"] == "aed99" for r in usage)
        assert all(e["agent"] == "claude" for e in k.read_jsonl(k.EVENT_LOG_NAME))

    def test_a_blank_agent_adds_no_keys_to_events(self, store):
        from typer.testing import CliRunner
        CliRunner().invoke(k.app, ["propose", "Argocd runs per cluster.",
                                   "--topic", "argocd", "--id", "p3", "--evidence", "e"])
        event = k.read_jsonl(k.EVENT_LOG_NAME)[0]
        assert "agent" not in event and "session_id" not in event


class TestRetrievalHealth:
    """The regression detector. Hit rate alone cannot tell "the floor moved"
    from "the corpus shrank", which is how a repo filter removed a third of
    eligible facts without anything looking broken."""

    def test_recall_records_corpus_shape_not_just_the_hit(self, store, monkeypatch):
        from typer.testing import CliRunner
        add_fact(store, "live", "Kargo promotes freight to a stamp.")
        add_fact(store, "dead", "Kargo promotes freight to a stamp.",
                 epistemic_status="refuted")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "")
        CliRunner().invoke(k.app, ["recall", "how does kargo promote freight", "--quiet"])
        row = [json.loads(x) for x in
               (store / k.USAGE_LOG_NAME).read_text().splitlines() if x.strip()][-1]
        assert row["eligible"] == 1 and row["total"] == 2
        assert row["excluded"] == {"status": 1}

    def test_a_miss_still_records_the_best_score_it_saw(self, store, monkeypatch):
        """Without this a floor set too high is indistinguishable from an empty
        store: both log hit=False and nothing else."""
        from typer.testing import CliRunner
        add_fact(store, "live", "Kargo promotes freight to a stamp.")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "")
        CliRunner().invoke(k.app, ["recall", "how does kargo promote freight",
                                   "--min-score", "0.99", "--quiet"])
        row = [json.loads(x) for x in
               (store / k.USAGE_LOG_NAME).read_text().splitlines() if x.strip()][-1]
        assert row["hit"] is False and row["top"] > 0.0 and row["eligible"] == 1

    def test_report_surfaces_an_eligibility_drop(self, store, monkeypatch):
        from typer.testing import CliRunner
        add_fact(store, "live", "Kargo promotes freight to a stamp.")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "")
        CliRunner().invoke(k.app, ["recall", "how does kargo promote freight", "--quiet"])
        rows = json.loads(CliRunner().invoke(
            k.app, ["retrieval-health", "--json"]).stdout)
        assert len(rows) == 1
        assert rows[0]["recalls"] == 1 and rows[0]["hit_rate"] == 1.0
        assert rows[0]["eligible"] == 1.0 and rows[0]["corpus"] == 1.0

    def test_report_buckets_by_agent(self, store, monkeypatch):
        from typer.testing import CliRunner
        add_fact(store, "live", "Kargo promotes freight to a stamp.")
        monkeypatch.setattr(k, "detect_repo", lambda cwd="": "")
        monkeypatch.setenv("KNOWLEDGE_AGENT", "claude")
        monkeypatch.setenv("KNOWLEDGE_SESSION_ID", "s1")
        CliRunner().invoke(k.app, ["recall", "how does kargo promote freight", "--quiet"])
        monkeypatch.setenv("KNOWLEDGE_AGENT", "codex")
        CliRunner().invoke(k.app, ["recall", "how does kargo promote freight", "--quiet"])
        rows = json.loads(CliRunner().invoke(
            k.app, ["retrieval-health", "--by", "agent", "--json"]).stdout)
        assert {r["bucket"] for r in rows} == {"claude", "codex"}

    def test_report_is_quiet_with_no_recalls(self, store):
        from typer.testing import CliRunner
        res = CliRunner().invoke(k.app, ["retrieval-health"])
        assert res.exit_code == 0 and "No recall events" in plain(res.stdout)

    def test_pending_filters_by_session_so_a_bad_batch_is_reviewable(self, store, monkeypatch):
        from typer.testing import CliRunner
        monkeypatch.setenv("KNOWLEDGE_AGENT", "claude")
        monkeypatch.setenv("KNOWLEDGE_SESSION_ID", "good")
        CliRunner().invoke(k.app, ["propose", "Argocd runs per cluster.",
                                   "--topic", "argocd", "--id", "ok", "--evidence", "e"])
        monkeypatch.setenv("KNOWLEDGE_SESSION_ID", "bad")
        CliRunner().invoke(k.app, ["propose", "Kargo promotes freight to stamps.",
                                   "--topic", "kargo", "--id", "oops", "--evidence", "e"])
        rows = json.loads(CliRunner().invoke(
            k.app, ["pending", "--session", "bad", "--json"]).stdout)
        assert [r["id"] for r in rows] == ["oops"]
        by_agent = json.loads(CliRunner().invoke(
            k.app, ["pending", "--agent", "claude", "--json"]).stdout)
        assert {r["id"] for r in by_agent} == {"ok", "oops"}
