"""Characterisation tests: load two small sources into a temporary DuckDB, dedupe, and write lists/parquet."""

from __future__ import annotations

import csv
import subprocess
import sys
import zipfile

import duckdb
import pandas as pd
import pytest

import build

CH_HEADER = [
    "CompanyName",
    "CompanyNumber",
    "RegAddress.PostTown",
    "CompanyStatus",
    "SICCode.SicText_1",
    "URI",
]
CH_ROWS = [
    ["ACME LTD", "00000001", "LONDON", "Active", "68100 - Buying and selling of own real estate", "http://x/1"],
    ["BETA PLC", "00000002", "LEEDS", "Active", "62012 - Business and domestic software development", "http://x/2"],
    ["GAMMA LIMITED", "00000003", "YORK", "Active", "62012 - Software", "http://x/3"],
    ["GAMMA LTD", "00000004", "YORK", "Active", "62012 - Software", "http://x/4"],
    ["OLD CO LTD", "00000005", "BATH", "Dissolved", "68100 - Real estate", "http://x/5"],
    ["NOCODE LTD", "00000006", "HULL", "Active", "", "http://x/6"],
]
DAMO_ROWS = [
    ("LSE:ACM", "Acme Limited (LSE:ACM)", "United Kingdom", "Real Estate (General/Diversified)"),
    ("LSE:GAM", "Gamma Ltd", "United Kingdom", "Software (Internet)"),
    ("LSE:SOLO", "Solo Listed plc", "United Kingdom", "Steel"),
    ("NSE:DIV", "Diversified Co", "India", "Diversified"),
]


@pytest.fixture
def ch_zip(workdir):
    raw = workdir / "raw"
    raw.mkdir()
    csv_path = raw / "BasicCompanyDataAsOneFile-2026-01-01.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CH_HEADER)
        w.writerows(CH_ROWS)
    zpath = raw / "BasicCompanyDataAsOneFile-2026-01-01.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(csv_path, csv_path.name)
    return zpath


@pytest.fixture
def damo_parquet(workdir):
    df = pd.DataFrame(DAMO_ROWS, columns=["Exchange:Ticker", "Company Name", "Country", "Industry Group"])
    path = workdir / "damodaran.parquet"
    df.to_parquet(path)
    return path


@pytest.fixture
def loaded(workdir, ch_zip, damo_parquet):
    build.load("ch", ch_zip)
    build.load("damodaran", damo_parquet)
    return duckdb.connect(str(workdir / "company_lists.duckdb"))


def rows(con, sql):
    return con.execute(sql).fetchall()


def test_load_ch_keeps_active_rows_and_maps_sic(loaded):
    got = rows(
        loaded,
        "SELECT source_id, name, city, sector_code, sector, status FROM companies WHERE source='ch' ORDER BY source_id",
    )
    assert [r[0] for r in got] == ["00000001", "00000002", "00000003", "00000004", "00000006"]
    assert got[0] == ("00000001", "ACME LTD", "LONDON", "68100", "Real estate", "Active")
    assert got[1][3:5] == ("62012", "Information & communication")
    assert got[4][3:5] == (None, None)  # empty SIC text loads as NULL


def test_load_damodaran_maps_country_and_sector(loaded):
    got = rows(
        loaded, "SELECT source_id, country, sector, status FROM companies WHERE source='damodaran' ORDER BY source_id"
    )
    assert got == [
        ("LSE:ACM", "GB", "Real estate", "Listed"),
        ("LSE:GAM", "GB", "Information & communication", "Listed"),
        ("LSE:SOLO", "GB", "Manufacturing", "Listed"),
        ("NSE:DIV", "IN", None, "Listed"),
    ]


def test_load_is_idempotent_per_source(loaded, damo_parquet):
    build.load("damodaran", damo_parquet)
    assert rows(loaded, "SELECT count(*) FROM companies WHERE source='damodaran'")[0][0] == 4


def test_dedupe_merges_listed_into_unique_registry_row(loaded):
    build.dedupe()
    got = {r[0]: r[1:] for r in rows(loaded, "SELECT source_id, sources, listed_ticker, sector FROM companies_dedup")}
    # unique registry match -> merged, gains ticker and sources
    assert got["00000001"] == ("ch+damodaran", "LSE:ACM", "Real estate")
    assert "LSE:ACM" not in got
    # two registry rows normalise to 'gamma' -> ambiguous, listed row kept separately
    assert got["00000003"][0] == "ch"
    assert got["00000004"][0] == "ch"
    assert got["LSE:GAM"] == ("damodaran", None, "Information & communication")
    # no registry match -> kept
    assert got["LSE:SOLO"][0] == "damodaran"
    assert got["NSE:DIV"][0] == "damodaran"
    assert len(got) == 8


@pytest.mark.xfail(
    strict=True, reason="db() re-registers UDFs on DuckDB's cached connection; second step in one process fails"
)
def test_lists_writes_one_csv_per_country_sector(loaded, workdir):
    build.dedupe()
    build.lists()
    files = sorted(p.relative_to(workdir).as_posix() for p in (workdir / "lists").rglob("*.csv"))
    assert files == [
        "lists/GB/information_communication.csv",
        "lists/GB/manufacturing.csv",
        "lists/GB/real_estate.csv",
        "lists/GB/unclassified.csv",
        "lists/IN/unclassified.csv",
    ]
    with (workdir / "lists/GB/real_estate.csv").open() as fh:
        recs = list(csv.DictReader(fh))
    assert [r["name"] for r in recs] == ["ACME LTD"]
    assert recs[0]["listed_ticker"] == "LSE:ACM"
    assert list(recs[0]) == [
        "name",
        "sources",
        "source_id",
        "city",
        "website",
        "sector_code",
        "size",
        "status",
        "listed_ticker",
    ]


def test_export_partitions_parquet(loaded, workdir):
    build.export()
    parts = sorted(
        p.parent.relative_to(workdir / "by_sector_country").as_posix()
        for p in (workdir / "by_sector_country").rglob("*.parquet")
    )
    assert parts == [
        "country=GB/sector=Information%20%26%20communication",
        "country=GB/sector=Manufacturing",
        "country=GB/sector=Real%20estate",
    ]


def test_cli_without_arguments_fails_nonzero():
    r = subprocess.run([sys.executable, str(build.__file__)], capture_output=True, text=True)
    assert r.returncode != 0
