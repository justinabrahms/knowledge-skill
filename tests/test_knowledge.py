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
import sys
from pathlib import Path

import json

import pytest
import yaml

CLI = Path(__file__).resolve().parent.parent / "bin" / "knowledge"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    """Strip styling and wrapping before matching a literal against CLI output.

    `console` is a plain `rich.Console()`, so highlighting is on and rich colours
    bare integers: "from 1 active facts" is emitted as
    "from \\x1b[1;36m1\\x1b[0m active facts". Whether it does depends on the
    environment -- rich only styles when it thinks something is watching -- so a
    literal assertion that straddles a number passes on a bare terminal and fails
    under anything that sets FORCE_COLOR, which every agent harness does. Neither
    `no_color` nor NO_COLOR is enough; they drop the colour and keep the bold.
    """
    return _ANSI.sub("", output).replace("\n", " ")


def _load_module():
    spec = importlib.util.spec_from_loader(
        "knowledge_cli", importlib.machinery.SourceFileLoader("knowledge_cli", str(CLI))
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["knowledge_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


k = _load_module()


# ---------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    """No test may read or write the developer's real user config.

    `find_config_file` falls back to USER_CONFIG when walking up finds nothing,
    so a test running in a bare tmp dir silently picked up whatever the
    developer had configured. That is not a hypothetical: `tune --write` wrote
    thresholds derived from these synthetic fixtures into a real
    ~/.config/knowledge/config.yml, replacing hand-written settings.
    """
    monkeypatch.setattr(k, "USER_CONFIG", tmp_path / "no-such-user-config.yml")
    k._CONFIG = None
    yield
    k._CONFIG = None


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An empty store, wired up so the module resolves to it."""
    s = tmp_path / "store"
    (s / "pending").mkdir(parents=True)
    monkeypatch.setenv("KNOWLEDGE_DIR", str(s))
    monkeypatch.chdir(tmp_path)
    k._STORE_OVERRIDE = None
    k._CONFIG = None
    k.load_config(reload=True)
    return s


def write_config(store: Path, **overrides) -> Path:
    cfg = {"store": ".", "scopes": ["org", "personal"], "prefixed_scopes": ["repo", "task"]}
    cfg.update(overrides)
    p = store / ".knowledge.yml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    k._CONFIG = None
    k.load_config(reload=True)
    return p


def add_fact(store: Path, fid: str, body: str, **fm) -> Path:
    front = {"id": fid, "topic": fm.get("topic", "t"), "scope": fm.get("scope", "org"),
             "learned_at": "2026-01-01", "last_verified_at": "2026-01-01",
             "valid_until": None, "invalidated_at": fm.get("invalidated_at"),
             "invalidation_reason": None, "confidence": fm.get("confidence", "high"),
             "source": "test"}
    p = store / f"{fid}.md"
    p.write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + body + "\n")
    return p


def add_candidate(store: Path, pid: str, text: str, **fields) -> Path:
    rec = {"id": pid, "text": text, "topic": fields.get("topic", "t"),
           "scope": fields.get("scope", "org"), "confidence": fields.get("confidence", "low"),
           "provenance": fields.get("provenance", "inferred"),
           "evidence": fields.get("evidence", ""), "source": "test",
           "proposed_at": "2026-01-01"}
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
