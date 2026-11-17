# Company database builder

Builds a country-wise, sector-wise universe of companies from free, open company registries (no scraping of
commercial contact databases) into a single DuckDB file and per-country, per-sector CSV lists. The last full
build covered 18.17M companies in 132 countries across 21 sectors (97.6% sector-coded). For anyone who needs a
reproducible, licence-clean list of companies by country and industry.

> Status: maintained snapshot. **Public copy of a private repository.** Left out of this copy and its history:
> the defence-industry sample (compiled from association member lists; redistribution terms unchecked) and the
> Estonia e-Business Register sample (licence not confirmed). The code that builds them is included. The data
> itself (1.2 GB of CSV + a 1.4 GB DuckDB) is not in git; `samples/` has 200 rows per shipped source and `docs/`
> has the row counts. Check each provider's terms in `SOURCES.md` before redistributing anything you rebuild.

## What it does
- Downloads the GLEIF LEI golden copy and the UK Companies House bulk file (`build.py fetch gleif|ch`).
- Loads ten source formats into one `companies` table with a unified sector column (`build.py load <source> <file>`).
- Maps every source's industry scheme (UK SIC 2007, NACE Rev2, NAF, EMTAK, SN2007, India NIC, Damodaran groups) onto
  the 21 UK SIC 2007 section names.
- Dedupes to one row per `(country, normalised name)`, merging listed/defence rows into registry rows and carrying
  the exchange ticker across (`build.py dedupe`).
- Writes `lists/<ISO2>/<sector>.csv` and a Parquet tree partitioned by country and sector (`build.py lists|export`).
- `fetch_fi.py` pulls Finland's PRH register page by page, resumably (the first run was blocked by the server).

## Quick start
```bash
git clone https://github.com/herrrickshaw/company-database-public.git
cd company-database-public
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 build.py fetch gleif && python3 build.py fetch ch
python3 build.py load gleif raw/gleif_lei2.csv.zip      # also: load in_mca raw/gleif_lei2.csv.zip
python3 build.py load ch raw/BasicCompanyDataAsOneFile-*.zip
python3 build.py load damodaran <damodaran_companies.parquet>
python3 build.py load fr_sirene <StockUniteLegale parquet>
python3 build.py load no_brreg <enheter csv.gz>
python3 build.py load ee_ariregister <13_11_yldandmed.parquet>
python3 build.py dedupe && python3 build.py lists
```
Each command is one process; `build.py` with no or wrong arguments prints the usage and exits 2.

## How it works
Everything lives in `build.py`; every stage reads and writes the same DuckDB file (`company_lists.duckdb`).

| Stage | Function(s) | Output |
|---|---|---|
| fetch | `fetch()` → `FETCHERS` (`fetch_gleif`, `fetch_ch`) | `raw/*.zip` |
| load | `load()` → `LOADERS` (`load_gleif`, `load_ch`, `load_damodaran`, `load_in_mca`, `load_iem_cin`, `load_defence`, `load_fr_sirene`, `load_ee_ariregister`, `load_no_brreg`, `load_pdl`) | table `companies` (rows of that source replaced) |
| sector mapping | `sic_sector()`, `nic_sector()`, `DAMO`, `cin_sector_sql()` (registered as DuckDB UDFs in `db()`) | `sector` column |
| dedupe | `dedupe()` | tables `norm`, `tgt`, `companies_dedup` |
| lists / export | `lists()`, `export()` | `lists/<ISO2>/<sector>.csv`, `by_sector_country/` Parquet |

Columns of `companies`: `source, source_id, name, country, city, website, sector_scheme, sector_code, sector, size, status`.
Columns of the lists: `name, sources, source_id, city, website, sector_code, size, status, listed_ticker`.

Design decisions:
- **DuckDB, not pandas**, for the registry loads: SIRENE and Companies House are millions of rows and DuckDB reads
  the CSV/Parquet directly with SQL; pandas is used only for the two small frames (Damodaran, defence).
- **Adding a source** means writing one `load_<name>(con, path)` function and adding it to `LOADERS`; `load()` and
  the CLI do not change.
- **Sector scheme.** The 21 UK SIC 2007 section names. NACE Rev2 / NAF Rev2 / EMTAK 2008 / SN2007 share SIC 2007's
  two-digit divisions, so they map directly (`sic_sector`). Other schemes are mapped approximately: Damodaran
  industry groups via the `DAMO` table, India NIC via the `NIC` table, NAF Rev1/1993 via the NIC-2004 table.
- **India.** The CIN company number embeds a 5-digit industry code. Companies incorporated **before 2023** use
  NIC-2004 (`NIC` table, approximate); **2023 onward** use NIC-2008, which matches SIC 2007 divisions
  (`NIC_2008_FROM_YEAR`). The 2023 break was found empirically (share of NIC-2008-only divisions jumps from ~3% to ~27%).
- **Dedupe rules.** Registry rows (`RANK` ≤ 2) always win. A listed or defence row merges into a registry row only
  when exactly one registry row has that normalised name; otherwise it is kept separately to avoid wrong merges.
  Merged rows gain `listed_ticker` and `sources`. GLEIF (3.46M entities) is loaded for identity but has no sector,
  so it is excluded from the lists.

## Repository layout
| Path | Purpose |
|---|---|
| `build.py` | the whole pipeline and its CLI (fetch, load, dedupe, lists, export) |
| `fetch_fi.py` | resumable, polite page fetcher for Finland's PRH API (data not yet loaded) |
| `tests/` | pytest: sector-map unit tests, loader/dedupe/lists/export characterisation tests on tiny fixtures |
| `samples/` | 200 rows per shipped source, in the list column layout (tracked) |
| `docs/` | `counts_by_source.csv`, `counts_by_country_sector.csv` from the last full build (tracked) |
| `SOURCES.md` | provider URLs and licences |
| `raw/`, `lists/`, `by_sector_country/`, `*.duckdb` | generated, gitignored |

## Data sources
Rows are from the last full build; see `SOURCES.md` for URLs and licences.

| Source | Rows | Notes |
|---|---|---|
| France SIRENE (INSEE) | 11,263,902 | active legal units with a name; ~3M are small property companies (SCI) |
| UK Companies House | 5,166,057 | active companies; SIC codes are self-reported |
| Norway Brønnøysund | 1,165,528 | excludes bankrupt / winding-up |
| Estonia e-Business Register | 351,198 | registered companies, EMTAK main activity |
| India (MCA CIN via GLEIF) | 170,010 | only companies holding an LEI, not the full MCA register |
| Damodaran listed companies | 43,125 | 132 countries, exchange-listed only |
| India IEM-linked CINs | 8,513 | companies in DPIIT IEM filings, not already above |
| Defence-industry registry | 2,477 | 16 countries; 2,266 have no industry code |

## Configuration
No environment variables and no secrets. Paths are relative to the repository (`HERE`, `RAW`, `DB` in `build.py`);
the library functions also accept an explicit DuckDB connection and output directory.

## Testing
```bash
pip install pytest ruff
ruff check . && pytest -q
```
Tests build a throw-away DuckDB in a temp directory from inline fixtures; nothing touches the network.

## Limitations and known issues
- Registry sector can mislead for holding companies (e.g. a software firm filed under Finance & insurance).
- Name-based matching can miss merges and, rarely, wrong-merge.
- **Not covered:** Finland (PRH API refused connections), Brazil, Denmark, Singapore, US SEC. Outside the registry
  countries only listed and defence companies are present.
- PDL's free company dataset (global, needs a request form) is supported by `build.py load pdl <file>` but untested.
- The `iem_cin` and `defence` inputs are private derived files; their loaders are included but the inputs are not.

## Licence
No licence file yet — all rights reserved for the code. The data each loader consumes carries its provider's
licence (see `SOURCES.md`).
