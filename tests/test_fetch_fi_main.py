"""fetch_fi: the success path of ``get`` and the ``main`` driver, with urllib and sleeping faked."""

from __future__ import annotations

import io
import json

import pytest

import fetch_fi


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _payload(n):
    return json.dumps({"companies": [{"id": i} for i in range(n)]}).encode()


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(fetch_fi.time, "sleep", calls.append)
    return calls


def test_get_writes_page_returns_row_count_and_pauses(tmp_path, monkeypatch, sleeps):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["ua"], seen["timeout"] = req.full_url, req.get_header("User-agent"), timeout
        return _Resp(_payload(3))

    monkeypatch.setattr(fetch_fi.urllib.request, "urlopen", fake_urlopen)
    assert fetch_fi.get(5, tmp_path) == (5, 3)
    assert (tmp_path / "page_5.json").read_bytes() == _payload(3)
    assert seen == {"url": fetch_fi.URL.format(5), "ua": "company-lists/1.0", "timeout": 60}
    assert sleeps == [fetch_fi.PAUSE_S]


def test_get_zero_rows_is_zero_not_failure(tmp_path, monkeypatch, sleeps):
    monkeypatch.setattr(fetch_fi.urllib.request, "urlopen", lambda *a, **k: _Resp(_payload(0)))
    assert fetch_fi.get(1, tmp_path) == (1, 0)
    assert (tmp_path / "page_1.json").exists()


def test_get_retries_with_exponential_backoff_then_succeeds(tmp_path, monkeypatch, sleeps):
    attempts = []

    def flaky(req, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise fetch_fi.urllib.error.URLError("boom")
        return _Resp(_payload(2))

    monkeypatch.setattr(fetch_fi.urllib.request, "urlopen", flaky)
    assert fetch_fi.get(2, tmp_path) == (2, 2)
    assert sleeps == [1, 2, fetch_fi.PAUSE_S]  # 2**0, 2**1, then the post-success pause


@pytest.mark.parametrize("body", [b"not json", b'{"nope": []}'])
def test_get_treats_bad_json_and_missing_key_as_retryable(tmp_path, monkeypatch, sleeps, body):
    monkeypatch.setattr(fetch_fi, "RETRIES", 2)
    monkeypatch.setattr(fetch_fi.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert fetch_fi.get(9, tmp_path) == (9, -1)
    assert [s for s in sleeps if s != fetch_fi.PAUSE_S] == [1, 2]  # back-off 2**0, 2**1 between attempts


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="fetch_fi.py:42 writes the page to disk before parsing it, so a bad response leaves a corrupt "
    "page_N.json behind and the resumable skip guard (line 36) never refetches it",
)
@pytest.mark.parametrize("body", [b"not json", b'{"nope": []}'])
def test_get_does_not_leave_a_corrupt_page_on_disk_after_failure(tmp_path, monkeypatch, sleeps, body):
    monkeypatch.setattr(fetch_fi, "RETRIES", 1)
    monkeypatch.setattr(fetch_fi.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert fetch_fi.get(4, tmp_path) == (4, -1)
    assert not (tmp_path / "page_4.json").exists()


def _fake_main_env(monkeypatch, tmp_path, total, results):
    monkeypatch.setattr(fetch_fi, "OUT", tmp_path / "raw" / "fi")
    monkeypatch.setattr(fetch_fi, "THREADS", 1)
    monkeypatch.setattr(
        fetch_fi.urllib.request, "urlopen", lambda url, *a, **k: _Resp(json.dumps({"totalResults": total}).encode())
    )
    fetched = []

    def fake_get(p):
        fetched.append(p)
        return p, results.get(p, 1)

    monkeypatch.setattr(fetch_fi, "get", fake_get)
    return fetched


def test_main_fetches_every_page_and_reports_clean_run(tmp_path, monkeypatch, capsys):
    fetched = _fake_main_env(monkeypatch, tmp_path, total=250, results={})
    assert fetch_fi.main() == 0
    assert fetched == [1, 2, 3]
    assert (tmp_path / "raw" / "fi").is_dir()
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "total 250 pages 3"
    assert out[1] == "0 pages done, fails 0"  # progress line printed on i % 500 == 0
    assert out[-1] == "done, fails 0"


def test_main_counts_failed_pages_but_still_exits_zero(tmp_path, monkeypatch, capsys):
    _fake_main_env(monkeypatch, tmp_path, total=300, results={2: -1, 3: -1})
    assert fetch_fi.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "total 300 pages 3"
    assert out[-1] == "done, fails 2"
    assert "0 pages done, fails 0" in out  # first page (i=0) was fine when the progress line printed


def test_main_prints_progress_every_500_pages(tmp_path, monkeypatch, capsys):
    _fake_main_env(monkeypatch, tmp_path, total=100 * 501, results={})
    fetch_fi.main()
    out = capsys.readouterr().out.splitlines()
    assert [ln for ln in out if "pages done" in ln] == ["0 pages done, fails 0", "500 pages done, fails 0"]


def test_main_does_not_count_empty_or_already_downloaded_pages_as_failures(tmp_path, monkeypatch, capsys):
    _fake_main_env(monkeypatch, tmp_path, total=300, results={2: 0, 3: 0})  # 0 rows = on disk already / empty page
    assert fetch_fi.main() == 0
    assert capsys.readouterr().out.splitlines()[-1] == "done, fails 0"
