"""build.main: the argument dispatcher, called in-process."""

from __future__ import annotations

import pytest

import build


@pytest.mark.parametrize("argv", [["fetch", "nope"], ["load", "nope", "file.csv"]])
def test_main_reports_unknown_source_on_stderr_and_returns_2(argv, workdir, capsys):
    assert build.main(argv) == 2
    cap = capsys.readouterr()
    assert cap.err.startswith("error: unknown ")
    assert "choose from" in cap.err
    assert cap.out == ""


@pytest.mark.parametrize("argv", [[], ["bogus"], ["fetch"], ["fetch", "gleif", "extra"], ["load", "ch"], ["load"]])
def test_main_prints_usage_on_bad_arguments(argv, workdir, capsys):
    assert build.main(argv) == 2
    cap = capsys.readouterr()
    assert cap.err.strip() == build.USAGE.strip()
    assert "build.py fetch gleif|ch" in cap.err
    assert cap.out == ""


def test_main_dispatches_each_verb_to_its_function(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "fetch", lambda w: calls.append(("fetch", w)))
    monkeypatch.setattr(build, "load", lambda w, p: calls.append(("load", w, p)))
    monkeypatch.setattr(build, "export", lambda: calls.append(("export",)))
    monkeypatch.setattr(build, "dedupe", lambda: calls.append(("dedupe",)))
    monkeypatch.setattr(build, "lists", lambda: calls.append(("lists",)))
    for argv in (["fetch", "ch"], ["load", "ch", "f.zip"], ["export"], ["dedupe"], ["lists"]):
        assert build.main(argv) == 0
    assert calls == [("fetch", "ch"), ("load", "ch", "f.zip"), ("export",), ("dedupe",), ("lists",)]
