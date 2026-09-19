"""Shared fixtures and helpers.

These lived in test_knowledge.py until a second test module needed them. The
module loader in particular has to stay in one place: the CLI is a `uv run`
script rather than an importable package, so it is loaded by path, and loading
it twice under two names would give each test file its own module-level config
cache to disagree over.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import sys
from pathlib import Path

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


def add_fact(store: Path, fid: str, body: str, **fm) -> Path:
    front = {"id": fid, "topic": fm.get("topic", "t"), "scope": fm.get("scope", "org"),
             "learned_at": "2026-01-01", "last_verified_at": "2026-01-01",
             "valid_until": None, "invalidated_at": fm.get("invalidated_at"),
             "invalidation_reason": None, "confidence": fm.get("confidence", "high"),
             "source": "test"}
    for key, value in fm.items():
        if key not in {"topic", "scope", "invalidated_at", "confidence"}:
            front[key] = value
    p = store / f"{fid}.md"
    p.write_text("---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n\n" + body + "\n")
    return p
