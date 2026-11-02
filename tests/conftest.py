"""Shared fixtures: a throw-away build directory so tests never touch the real DuckDB file."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build  # noqa: E402


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Point every module-level path in build.py at tmp_path and return it."""
    monkeypatch.setattr(build, "HERE", tmp_path)
    monkeypatch.setattr(build, "RAW", tmp_path / "raw")
    monkeypatch.setattr(build, "DB", tmp_path / "company_lists.duckdb")
    return tmp_path
