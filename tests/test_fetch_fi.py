"""fetch_fi: importable without network; page maths and the resume guard are pure."""

from __future__ import annotations

import pytest

import fetch_fi


@pytest.mark.parametrize(("total", "pages"), [(0, 0), (1, 1), (100, 1), (101, 2), (1234, 13)])
def test_page_count(total, pages):
    assert fetch_fi.page_count(total) == pages


def test_get_skips_pages_already_on_disk(tmp_path, monkeypatch):
    (tmp_path / "page_7.json").write_text("{}")
    monkeypatch.setattr(fetch_fi.urllib.request, "urlopen", lambda *a, **k: pytest.fail("must not hit the network"))
    assert fetch_fi.get(7, tmp_path) == (7, 0)


def test_get_returns_minus_one_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_fi, "RETRIES", 2)
    monkeypatch.setattr(fetch_fi.time, "sleep", lambda s: None)

    def boom(*a, **k):
        raise fetch_fi.urllib.error.URLError("down")

    monkeypatch.setattr(fetch_fi.urllib.request, "urlopen", boom)
    assert fetch_fi.get(3, tmp_path) == (3, -1)
    assert not (tmp_path / "page_3.json").exists()
