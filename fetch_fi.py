#!/usr/bin/env python3
"""Pull Finland's PRH open-data company register (100/page) into raw/fi/page_N.json.

Resumable (pages already on disk are skipped); 8 threads with a short pause per request and exponential back-off.
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OUT = pathlib.Path(__file__).parent / "raw" / "fi"
URL = "https://avoindata.prh.fi/opendata-ytj-api/v3/companies?page={}"
USER_AGENT = "company-lists/1.0"
PAGE_SIZE = 100
THREADS = 8
RETRIES = 6
TIMEOUT_S = 60
PAUSE_S = 0.15
PROGRESS_EVERY = 500


def page_count(total: int, page_size: int = PAGE_SIZE) -> int:
    """Number of pages needed for ``total`` rows (ceiling division)."""
    return -(-total // page_size)


def get(p: int, out: pathlib.Path = OUT) -> tuple[int, int]:
    """Fetch page ``p`` into ``out``. Returns (page, rows) — rows 0 if already on disk, -1 after RETRIES failures."""
    f = out / f"page_{p}.json"
    if f.exists():
        return p, 0
    req = urllib.request.Request(URL.format(p), headers={"User-Agent": USER_AGENT})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                data = resp.read()
            f.write_bytes(data)
            time.sleep(PAUSE_S)
            return p, len(json.loads(data)["companies"])
        except (urllib.error.URLError, OSError, ValueError, KeyError):  # network, HTTP, timeout, bad JSON
            time.sleep(2**attempt)
    return p, -1


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(URL.format(1)) as resp:
        total = json.load(resp)["totalResults"]
    pages = page_count(total)
    print("total", total, "pages", pages, flush=True)
    fails = 0
    with ThreadPoolExecutor(THREADS) as ex:
        for i, (_p, n) in enumerate(ex.map(get, range(1, pages + 1))):
            if n < 0:
                fails += 1
            if i % PROGRESS_EVERY == 0:
                print(i, "pages done, fails", fails, flush=True)
    print("done, fails", fails)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
