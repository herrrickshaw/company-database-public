#!/usr/bin/env python3
"""Free company universe -> DuckDB (company_lists.duckdb).

Usage:
    build.py fetch gleif|ch
    build.py load <source> <file>        (sources: see LOADERS)
    build.py dedupe
    build.py lists
    build.py export

Table ``companies``: source, source_id, name, country, city, website, sector_scheme, sector_code, sector, size, status.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import sys
import urllib.request
import zipfile
from collections.abc import Callable, Iterable

import duckdb

try:
    from duckdb.sqltypes import VARCHAR
except ImportError:  # duckdb < 1.2
    from duckdb.typing import VARCHAR

HERE = pathlib.Path(__file__).parent
RAW = HERE / "raw"
DB = HERE / "company_lists.duckdb"

GLEIF_LATEST_URL = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest"
CH_INDEX_URL = "https://download.companieshouse.gov.uk/en_output.html"
CH_FILE_RE = r"BasicCompanyDataAsOneFile-[\d-]+\.zip"
# GLEIF registration-authority code for India's Ministry of Corporate Affairs.
MCA_REGISTRATION_AUTHORITY = "RA000394"
# Indian CIN: L/U + 5-digit NIC + 2-letter state + 4-digit year + 3-letter type + 6-digit serial.
CIN_RE = r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$"
# Companies incorporated from this year on carry NIC-2008 codes (== UK SIC 2007 divisions); earlier ones NIC-2004.
NIC_2008_FROM_YEAR = 2023

# UK SIC 2007 division -> section name (first 2 digits)
SIC = [
    (1, 3, "Agriculture, forestry & fishing"),
    (5, 9, "Mining & quarrying"),
    (10, 33, "Manufacturing"),
    (35, 35, "Energy & utilities"),
    (36, 39, "Water, waste & remediation"),
    (41, 43, "Construction"),
    (45, 47, "Wholesale & retail"),
    (49, 53, "Transport & storage"),
    (55, 56, "Hospitality"),
    (58, 63, "Information & communication"),
    (64, 66, "Finance & insurance"),
    (68, 68, "Real estate"),
    (69, 75, "Professional & scientific"),
    (77, 82, "Admin & support services"),
    (84, 84, "Public administration"),
    (85, 85, "Education"),
    (86, 88, "Health & social work"),
    (90, 93, "Arts & recreation"),
    (94, 96, "Other services"),
    (97, 98, "Households"),
    (99, 99, "Extraterritorial"),
]

# India NIC-2004 division (first 2 digits of the 5-digit code embedded in the CIN) -> unified sector (approximate)
NIC = [
    (1, 5, "Agriculture, forestry & fishing"),
    (10, 14, "Mining & quarrying"),
    (15, 37, "Manufacturing"),
    (40, 40, "Energy & utilities"),
    (41, 41, "Water, waste & remediation"),
    (45, 45, "Construction"),
    (50, 52, "Wholesale & retail"),
    (55, 55, "Hospitality"),
    (60, 63, "Transport & storage"),
    (64, 64, "Information & communication"),
    (65, 67, "Finance & insurance"),
    (70, 70, "Real estate"),
    (71, 71, "Admin & support services"),
    (72, 72, "Information & communication"),
    (73, 74, "Professional & scientific"),
    (75, 75, "Public administration"),
    (80, 80, "Education"),
    (85, 85, "Health & social work"),
    (90, 90, "Water, waste & remediation"),
    (91, 91, "Other services"),
    (92, 93, "Arts & recreation"),
    (95, 99, "Households"),
]


def _lookup(table: Iterable[tuple[int, int, str]], division: int) -> str | None:
    return next((name for lo, hi, name in table if lo <= division <= hi), None)


def sic_sector(code: object) -> str | None:
    """Map a SIC-2007 / NACE-Rev2 code (string or int; first two digits used) to its section name."""
    try:
        division = int(str(code).strip()[:2])
    except (ValueError, TypeError):
        return None
    return _lookup(SIC, division)


def nic_sector(division: int) -> str | None:
    """Map a NIC-2004 two-digit division to the unified sector name."""
    return _lookup(NIC, division)


# Damodaran industry group -> unified sector (UK SIC 2007 section names)
MANUF, TRANSPORT, INFO, FINANCE, PROF, RETAIL, REALEST = (
    "Manufacturing",
    "Transport & storage",
    "Information & communication",
    "Finance & insurance",
    "Professional & scientific",
    "Wholesale & retail",
    "Real estate",
)
DAMO = {
    "Advertising": PROF,
    "Aerospace/Defense": MANUF,
    "Air Transport": TRANSPORT,
    "Apparel": MANUF,
    "Auto & Truck": MANUF,
    "Auto Parts": MANUF,
    "Bank (Money Center)": FINANCE,
    "Banks (Regional)": FINANCE,
    "Beverage (Alcoholic)": MANUF,
    "Beverage (Soft)": MANUF,
    "Broadcasting": INFO,
    "Brokerage & Investment Banking": FINANCE,
    "Building Materials": MANUF,
    "Business & Consumer Services": "Admin & support services",
    "Cable TV": INFO,
    "Chemical (Basic)": MANUF,
    "Chemical (Diversified)": MANUF,
    "Chemical (Specialty)": MANUF,
    "Coal & Related Energy": "Mining & quarrying",
    "Computer Services": INFO,
    "Computers/Peripherals": MANUF,
    "Construction Supplies": MANUF,
    "Diversified": None,
    "Drugs (Biotechnology)": MANUF,
    "Drugs (Pharmaceutical)": MANUF,
    "Education": "Education",
    "Electrical Equipment": MANUF,
    "Electronics (Consumer & Office)": MANUF,
    "Electronics (General)": MANUF,
    "Engineering/Construction": "Construction",
    "Entertainment": "Arts & recreation",
    "Environmental & Waste Services": "Water, waste & remediation",
    "Farming/Agriculture": "Agriculture, forestry & fishing",
    "Financial Svcs. (Non-bank & Insurance)": FINANCE,
    "Food Processing": MANUF,
    "Food Wholesalers": RETAIL,
    "Furn/Home Furnishings": MANUF,
    "Green & Renewable Energy": "Energy & utilities",
    "Healthcare Products": MANUF,
    "Healthcare Support Services": "Health & social work",
    "Heathcare Information and Technology": INFO,
    "Homebuilding": "Construction",
    "Hospitals/Healthcare Facilities": "Health & social work",
    "Hotel/Gaming": "Hospitality",
    "Household Products": MANUF,
    "Information Services": INFO,
    "Insurance (General)": FINANCE,
    "Insurance (Life)": FINANCE,
    "Insurance (Prop/Cas.)": FINANCE,
    "Investments & Asset Management": FINANCE,
    "Machinery": MANUF,
    "Metals & Mining": "Mining & quarrying",
    "Office Equipment & Services": MANUF,
    "Oil/Gas (Integrated)": "Mining & quarrying",
    "Oil/Gas (Production and Exploration)": "Mining & quarrying",
    "Oil/Gas Distribution": "Energy & utilities",
    "Oilfield Svcs/Equip.": "Mining & quarrying",
    "Packaging & Container": MANUF,
    "Paper/Forest Products": MANUF,
    "Power": "Energy & utilities",
    "Precious Metals": "Mining & quarrying",
    "Publishing & Newspapers": INFO,
    "R.E.I.T.": REALEST,
    "Real Estate (Development)": REALEST,
    "Real Estate (General/Diversified)": REALEST,
    "Real Estate (Operations & Services)": REALEST,
    "Recreation": "Arts & recreation",
    "Reinsurance": FINANCE,
    "Restaurant/Dining": "Hospitality",
    "Retail (Automotive)": RETAIL,
    "Retail (Building Supply)": RETAIL,
    "Retail (Distributors)": RETAIL,
    "Retail (General)": RETAIL,
    "Retail (Grocery and Food)": RETAIL,
    "Retail (REITs)": REALEST,
    "Retail (Special Lines)": RETAIL,
    "Rubber& Tires": MANUF,
    "Semiconductor": MANUF,
    "Semiconductor Equip": MANUF,
    "Shipbuilding & Marine": MANUF,
    "Shoe": MANUF,
    "Software (Entertainment)": INFO,
    "Software (Internet)": INFO,
    "Software (System & Application)": INFO,
    "Steel": MANUF,
    "Telecom (Wireless)": INFO,
    "Telecom. Equipment": MANUF,
    "Telecom. Services": INFO,
    "Tobacco": MANUF,
    "Transportation": TRANSPORT,
    "Transportation (Railroads)": TRANSPORT,
    "Trucking": TRANSPORT,
    "Utility (General)": "Energy & utilities",
    "Utility (Water)": "Water, waste & remediation",
}

# Country name as spelled in the Damodaran list -> ISO 3166-1 alpha-2
ISO = {
    "Argentina": "AR",
    "Australia": "AU",
    "Austria": "AT",
    "Azerbaijan": "AZ",
    "Bahamas": "BS",
    "Bahrain": "BH",
    "Bangladesh": "BD",
    "Barbados": "BB",
    "Belgium": "BE",
    "Benin": "BJ",
    "Bermuda": "BM",
    "Botswana": "BW",
    "Brazil": "BR",
    "British Virgin Islands": "VG",
    "Bulgaria": "BG",
    "Burkina Faso": "BF",
    "Cambodia": "KH",
    "Canada": "CA",
    "Cayman Islands": "KY",
    "Chile": "CL",
    "China": "CN",
    "Colombia": "CO",
    "Costa Rica": "CR",
    "Croatia": "HR",
    "Curaçao": "CW",
    "Cyprus": "CY",
    "Czech Republic": "CZ",
    "Denmark": "DK",
    "Dominican Republic": "DO",
    "Ecuador": "EC",
    "Egypt": "EG",
    "El Salvador": "SV",
    "Estonia": "EE",
    "Finland": "FI",
    "France": "FR",
    "Gabon": "GA",
    "Georgia": "GE",
    "Germany": "DE",
    "Ghana": "GH",
    "Gibraltar": "GI",
    "Greece": "GR",
    "Greenland": "GL",
    "Guernsey": "GG",
    "Hong Kong": "HK",
    "Hungary": "HU",
    "Iceland": "IS",
    "India": "IN",
    "Indonesia": "ID",
    "Ireland": "IE",
    "Isle of Man": "IM",
    "Israel": "IL",
    "Italy": "IT",
    "Ivory Coast": "CI",
    "Jamaica": "JM",
    "Japan": "JP",
    "Jersey": "JE",
    "Jordan": "JO",
    "Kazakhstan": "KZ",
    "Kenya": "KE",
    "Kuwait": "KW",
    "Latvia": "LV",
    "Liberia": "LR",
    "Liechtenstein": "LI",
    "Lithuania": "LT",
    "Luxembourg": "LU",
    "Macau": "MO",
    "Macedonia": "MK",
    "Malawi": "MW",
    "Malaysia": "MY",
    "Mali": "ML",
    "Malta": "MT",
    "Marshall Islands": "MH",
    "Martinique": "MQ",
    "Mauritius": "MU",
    "Mexico": "MX",
    "Moldova": "MD",
    "Monaco": "MC",
    "Mongolia": "MN",
    "Montenegro": "ME",
    "Morocco": "MA",
    "Myanmar": "MM",
    "Namibia": "NA",
    "Netherlands": "NL",
    "New Zealand": "NZ",
    "Niger": "NE",
    "Nigeria": "NG",
    "Norway": "NO",
    "Oman": "OM",
    "Pakistan": "PK",
    "Palestinian Authority": "PS",
    "Panama": "PA",
    "Papua New Guinea": "PG",
    "Peru": "PE",
    "Philippines": "PH",
    "Poland": "PL",
    "Portugal": "PT",
    "Qatar": "QA",
    "Romania": "RO",
    "Russia": "RU",
    "Rwanda": "RW",
    "Saint Lucia": "LC",
    "Saudi Arabia": "SA",
    "Senegal": "SN",
    "Serbia": "RS",
    "Singapore": "SG",
    "Slovakia": "SK",
    "Slovenia": "SI",
    "South Africa": "ZA",
    "South Korea": "KR",
    "Spain": "ES",
    "Sri Lanka": "LK",
    "Sudan": "SD",
    "Sweden": "SE",
    "Switzerland": "CH",
    "Taiwan": "TW",
    "Tanzania": "TZ",
    "Thailand": "TH",
    "Togo": "TG",
    "Trinidad & Tobago": "TT",
    "Tunisia": "TN",
    "Turkey": "TR",
    "Turks & Caicos Islands": "TC",
    "Uganda": "UG",
    "Ukraine": "UA",
    "United Arab Emirates": "AE",
    "United Kingdom": "GB",
    "United States": "US",
    "Uruguay": "UY",
    "Venezuela": "VE",
    "Vietnam": "VN",
    "Zambia": "ZM",
    "Zimbabwe": "ZW",
}

COLUMNS = (
    "source",
    "source_id",
    "name",
    "country",
    "city",
    "website",
    "sector_scheme",
    "sector_code",
    "sector",
    "size",
    "status",
)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db(path: pathlib.Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the DuckDB file, register the sector UDFs and ensure ``companies`` exists."""
    c = duckdb.connect(str(path or DB))
    c.create_function("nic_sector", nic_sector, ["INTEGER"], VARCHAR, null_handling="special")
    c.create_function("sic_sector", sic_sector, [VARCHAR], VARCHAR, null_handling="special")
    c.execute(
        """CREATE TABLE IF NOT EXISTS companies(source VARCHAR, source_id VARCHAR, name VARCHAR, country VARCHAR,
        city VARCHAR, website VARCHAR, sector_scheme VARCHAR, sector_code VARCHAR, sector VARCHAR, size VARCHAR, status VARCHAR)"""
    )
    return c


def _insert_frame(c: duckdb.DuckDBPyConnection, frame) -> None:
    """Append a pandas frame with exactly the ``companies`` columns."""
    c.register("o", frame)
    c.execute("INSERT INTO companies SELECT * FROM o")
    c.unregister("o")


# ---------------------------------------------------------------------------
# Fetch (download raw files)
# ---------------------------------------------------------------------------
def dl(url: str, dest: pathlib.Path) -> pathlib.Path:
    """Download ``url`` to ``dest`` and return ``dest``."""
    print("downloading", url)
    urllib.request.urlretrieve(url, dest)
    return dest


def fetch_gleif() -> pathlib.Path:
    """Latest GLEIF LEI2 golden-copy CSV (zipped)."""
    with urllib.request.urlopen(GLEIF_LATEST_URL) as resp:
        meta = json.load(resp)
    return dl(meta["data"]["full_file"]["csv"]["url"], RAW / "gleif_lei2.csv.zip")


def fetch_ch() -> pathlib.Path:
    """Companies House basic company data, single-file zip (name found on the index page)."""
    with urllib.request.urlopen(CH_INDEX_URL) as resp:
        html = resp.read().decode()
    name = re.search(CH_FILE_RE, html).group(0)
    return dl("https://download.companieshouse.gov.uk/" + name, RAW / name)


FETCHERS: dict[str, Callable[[], pathlib.Path]] = {"gleif": fetch_gleif, "ch": fetch_ch}


def fetch(what: str) -> pathlib.Path:
    """Download one raw source into ``raw/``."""
    if what not in FETCHERS:
        raise ValueError(f"unknown fetch source {what!r}; choose from {sorted(FETCHERS)}")
    RAW.mkdir(exist_ok=True)
    return FETCHERS[what]()


def unzip(path: str | pathlib.Path) -> pathlib.Path:
    """Extract a zip next to the raw file and return the first CSV inside."""
    out = RAW / (pathlib.Path(path).stem + "_x")
    out.mkdir(exist_ok=True)
    zipfile.ZipFile(path).extractall(out)
    return next(out.glob("*.csv"))


# ---------------------------------------------------------------------------
# Load (one function per source; each inserts into ``companies``)
# ---------------------------------------------------------------------------
def cin_sector_sql(cin: str = "cin") -> str:
    """SQL expression mapping a CIN column to a sector: NIC-2008 (== SIC) from 2023, NIC-2004 before."""
    return (
        f"CASE WHEN try_cast(substr({cin},9,4) AS INTEGER) >= {NIC_2008_FROM_YEAR} "
        f"THEN sic_sector(substr({cin},2,5)) ELSE nic_sector(try_cast(substr({cin},2,2) AS INTEGER)) END"
    )


def load_gleif(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    f = unzip(path)
    c.execute(
        f"""INSERT INTO companies SELECT 'gleif', LEI, "Entity.LegalName", "Entity.LegalAddress.Country",
          "Entity.LegalAddress.City", NULL, NULL, NULL, NULL, NULL, "Entity.EntityStatus"
          FROM read_csv('{f}', all_varchar=true, header=true)"""
    )


def load_ch(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    f = unzip(path)
    c.execute(
        f"""INSERT INTO companies SELECT 'ch', companynumber, companyname, 'GB', regaddressposttown, uri,
          'UK SIC 2007', split_part(siccodesictext_1,' - ',1), sic_sector(split_part(siccodesictext_1,' - ',1)), NULL, companystatus
          FROM read_csv('{f}', all_varchar=true, header=true, normalize_names=true, ignore_errors=true)
          WHERE companystatus='Active'"""
    )


def damodaran_frame(df):
    """Damodaran company list (columns Exchange:Ticker, Company Name, Country, Industry Group) -> companies rows."""
    import pandas as pd

    return pd.DataFrame(
        {
            "source": "damodaran",
            "source_id": df["Exchange:Ticker"],
            "name": df["Company Name"],
            "country": df["Country"].map(ISO),
            "city": None,
            "website": None,
            "sector_scheme": "Damodaran industry group",
            "sector_code": df["Industry Group"],
            "sector": df["Industry Group"].map(DAMO),
            "size": None,
            "status": "Listed",
        }
    )


def load_damodaran(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    import pandas as pd

    _insert_frame(c, damodaran_frame(pd.read_parquet(path)))


def load_in_mca(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    """Indian companies holding an LEI: the MCA CIN is GLEIF's registration-authority entity id."""
    f = unzip(path)
    c.execute(
        f"""INSERT INTO companies
          SELECT 'in_mca', cin, "Entity.LegalName", 'IN', "Entity.LegalAddress.City", NULL, 'NIC (from CIN)', substr(cin,2,5),
                 {cin_sector_sql()}, NULL, "Entity.EntityStatus"
          FROM (SELECT *, "Entity.RegistrationAuthority.RegistrationAuthorityEntityID" AS cin
                FROM read_csv('{f}', all_varchar=true, header=true))
          WHERE "Entity.RegistrationAuthority.RegistrationAuthorityID"='{MCA_REGISTRATION_AUTHORITY}'
            AND regexp_matches(cin, '{CIN_RE}')"""
    )


def load_iem_cin(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    """IEM-linked Indian companies with a CIN (parquet/CSV with cin, company_name, mca_status, month); skips CINs already in in_mca."""
    c.execute(
        f"""INSERT INTO companies
          SELECT 'iem_cin', cin, name, 'IN', NULL, NULL, 'NIC (from CIN)', substr(cin,2,5),
                 {cin_sector_sql()},
                 NULL, mca_status
          FROM (SELECT cin, arg_max(company_name, month) AS name, arg_max(mca_status, month) AS mca_status
                FROM '{path}' WHERE cin IS NOT NULL AND regexp_matches(cin, '{CIN_RE}') GROUP BY cin)
          WHERE cin NOT IN (SELECT source_id FROM companies WHERE source='in_mca')"""
    )


def defence_sector(value: object) -> str | None:
    """'NAF 25', 'SIC 3312', 'KSIC 30' ... -> sector via the two-digit division; anything else -> None."""
    m = re.match(r"^(NAF|SIC|KSIC)\s+(\d{2})", str(value))
    return sic_sector(m.group(2)) if m else None


def defence_frame(t):
    """Defence-registry TSV (registry_id, company, country, source_url, industry_code, size_signal) -> companies rows."""
    import pandas as pd

    iso = dict(ISO, **{"UK": "GB", "US": "US"})
    return pd.DataFrame(
        {
            "source": "defence",
            "source_id": t["registry_id"].astype(str),
            "name": t["company"],
            "country": t["country"].map(iso),
            "city": None,
            "website": t["source_url"],
            "sector_scheme": "defence registry",
            "sector_code": t["industry_code"],
            "sector": t["industry_code"].map(defence_sector),
            "size": t["size_signal"],
            "status": "defence-sector",
        }
    )


def load_defence(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    import pandas as pd

    _insert_frame(c, defence_frame(pd.read_csv(path, sep="\t")))


def load_fr_sirene(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    """NAF Rev2 (= NACE Rev2 = UK SIC 2007 divisions); NAF1993/Rev1 (= NACE 1.1 ~ NIC-2004) via nic_sector; NAP skipped."""
    c.execute(
        f"""INSERT INTO companies
          SELECT 'fr_sirene', siren, denominationUniteLegale, 'FR', NULL, NULL,
                 'NAF ' || coalesce(nomenclatureActivitePrincipaleUniteLegale,'?'), activitePrincipaleUniteLegale,
                 CASE nomenclatureActivitePrincipaleUniteLegale
                   WHEN 'NAFRev2' THEN sic_sector(substr(activitePrincipaleUniteLegale,1,2))
                   WHEN 'NAFRev1' THEN nic_sector(try_cast(substr(activitePrincipaleUniteLegale,1,2) AS INTEGER))
                   WHEN 'NAF1993' THEN nic_sector(try_cast(substr(activitePrincipaleUniteLegale,1,2) AS INTEGER)) END,
                 categorieEntreprise, 'Active'
          FROM '{path}' WHERE etatAdministratifUniteLegale='A' AND denominationUniteLegale IS NOT NULL"""
    )


def load_ee_ariregister(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    """EMTAK 2008 == NACE Rev2 == UK SIC 2007 divisions. Main activity from notified activities, else latest annual-report activity."""
    c.execute(
        f"""INSERT INTO companies
          SELECT 'ee_ariregister', CAST(ariregistri_kood AS VARCHAR), nimi, 'EE', NULL, NULL, 'EMTAK 2008', code, sic_sector(substr(code,1,2)), NULL, 'Registered'
          FROM (SELECT ariregistri_kood, nimi, yldandmed.staatus AS st,
                  coalesce(list_filter(yldandmed.teatatud_tegevusalad, x -> x.on_pohitegevusala AND x.lopp_kpv IS NULL)[1].emtak_kood,
                           list_filter(yldandmed.teatatud_tegevusalad, x -> x.lopp_kpv IS NULL)[1].emtak_kood,
                           yldandmed.info_majandusaasta_aruannetest[-1].tegevusala_emtak_kood) AS code
                FROM '{path}') WHERE st='R'"""
    )


def load_no_brreg(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    """Norway: naeringskode1 is NACE Rev2 (SN2007) == UK SIC 2007 divisions; skip bankrupt / winding-up / deleted."""
    c.execute(
        f"""INSERT INTO companies
          SELECT 'no_brreg', organisasjonsnummer, navn, 'NO', "forretningsadresse.poststed", NULLIF(hjemmeside,''),
                 'NACE (SN2007)', "naeringskode1.kode", sic_sector(substr("naeringskode1.kode",1,2)), antallAnsatte, "organisasjonsform.kode"
          FROM read_csv('{path}', all_varchar=true, header=true, ignore_errors=true)
          WHERE coalesce(konkurs,'false')<>'true' AND coalesce(underAvvikling,'false')<>'true'
            AND coalesce(underTvangsavviklingEllerTvangsopplosning,'false')<>'true'"""
    )


def load_pdl(c: duckdb.DuckDBPyConnection, path: str | pathlib.Path) -> None:
    """PDL free company dataset: pipe/CSV/JSON; id,name,website,industry,size,country,locality,... (untested)."""
    c.execute(
        f"""INSERT INTO companies SELECT 'pdl', id, name, country, locality, website, 'PDL industry', NULL,
          industry, size, NULL FROM read_csv_auto('{path}', all_varchar=true, ignore_errors=true)"""
    )


LOADERS: dict[str, Callable[[duckdb.DuckDBPyConnection, str | pathlib.Path], None]] = {
    "gleif": load_gleif,
    "ch": load_ch,
    "damodaran": load_damodaran,
    "in_mca": load_in_mca,
    "iem_cin": load_iem_cin,
    "defence": load_defence,
    "fr_sirene": load_fr_sirene,
    "ee_ariregister": load_ee_ariregister,
    "no_brreg": load_no_brreg,
    "pdl": load_pdl,
}


def load(what: str, path: str | pathlib.Path, con: duckdb.DuckDBPyConnection | None = None) -> int:
    """Replace all rows of source ``what`` from ``path``; returns the new row count for that source."""
    if what not in LOADERS:
        raise ValueError(f"unknown source {what!r}; choose from {sorted(LOADERS)}")
    c = con or db()
    c.execute("DELETE FROM companies WHERE source=?", [what])
    LOADERS[what](c, path)
    n = c.execute("SELECT count(*) FROM companies WHERE source=?", [what]).fetchone()[0]
    print(what, n, "rows")
    return n


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------
# SQL: lower-case, strip accents/punctuation/parenthesised parts, '&' -> 'and', drop trailing legal-form suffixes and leading 'the'.
NORM = r"""regexp_replace(trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
  regexp_replace(replace(replace(lower(strip_accents(name)), '.', ''), '''', ''), '[(].*?[)]', ' ', 'g'), '&', ' and ', 'g'), '[^a-z0-9 ]', ' ', 'g'), ' +', ' ', 'g'),
  '( (the|ltd|limited|pvt|private|plc|public|inc|incorporated|corp|corporation|co|company|llc|llp|sa|ag|gmbh|nv|bv|ab|as|oy|spa|sdn|bhd|tbk|kk|jsc|pjsc|lp|cv))+ *$', '', 'g')), '^the ', '')"""

# Lower rank wins a merge. rank<=2 are registries and are never merged away; GLEIF is absent (no sector).
RANK = {
    "ch": 1,
    "in_mca": 1,
    "fr_sirene": 1,
    "no_brreg": 1,
    "ee_ariregister": 1,
    "fi_prh": 1,
    "iem_cin": 2,
    "damodaran": 3,
    "defence": 4,
}
REGISTRY_MAX_RANK = 2


def dedupe(con: duckdb.DuckDBPyConnection | None = None) -> None:
    """One row per (country, normalised name) -> table ``companies_dedup``.

    Registry rows (rank<=2) win; a non-registry row merges into a registry row only if exactly one registry row has
    that name (else it stays, avoiding wrong merges). Among non-registry rows the lowest rank wins. GLEIF is excluded
    (no sector). Kept rows gain listed_ticker, sources, and a filled sector.
    """
    c = con or db()
    rank_case = " ".join(f"WHEN '{k}' THEN {v}" for k, v in RANK.items())
    sources = ",".join(repr(k) for k in RANK)
    c.execute(
        f"""CREATE OR REPLACE TABLE norm AS SELECT rowid AS rid, source, country, {NORM} AS nn,
        CASE source {rank_case} END AS rnk
        FROM companies WHERE source IN ({sources}) AND name IS NOT NULL"""
    )
    c.execute(
        f"""CREATE OR REPLACE TABLE tgt AS
      SELECT n.rid, n.source, CASE WHEN n.nn='' OR n.country IS NULL THEN n.rid WHEN n.rnk<={REGISTRY_MAX_RANK} THEN n.rid
             WHEN rs.rid IS NOT NULL THEN rs.rid WHEN rc.cnt>0 THEN n.rid ELSE fn.rid END AS target
      FROM norm n
      LEFT JOIN (SELECT country,nn,min(rid) rid FROM norm WHERE rnk<={REGISTRY_MAX_RANK} GROUP BY 1,2 HAVING count(*)=1) rs USING(country,nn)
      LEFT JOIN (SELECT country,nn,count(*) cnt FROM norm WHERE rnk<={REGISTRY_MAX_RANK} GROUP BY 1,2) rc USING(country,nn)
      LEFT JOIN (SELECT country,nn,arg_min(rid, CAST(rnk AS BIGINT)*1000000000+rid) rid FROM norm WHERE rnk>{REGISTRY_MAX_RANK} GROUP BY 1,2) fn USING(country,nn)"""
    )
    c.execute(
        """CREATE OR REPLACE TABLE companies_dedup AS
      SELECT k.source, k.source_id, k.name, k.country, k.city, k.website, k.sector_scheme, k.sector_code,
             coalesce(k.sector, m.sec) AS sector, k.size, k.status, m.tick AS listed_ticker,
             concat_ws('+', k.source, m.alsoin) AS sources
      FROM (SELECT rowid AS rid, * FROM companies) k
      JOIN tgt t ON t.rid=k.rid AND t.target=t.rid
      LEFT JOIN (SELECT t2.target, string_agg(CASE WHEN c2.source='damodaran' THEN c2.source_id END, ', ') tick,
                        array_to_string(list(DISTINCT c2.source), '+') alsoin, min(c2.sector) sec
                 FROM tgt t2 JOIN (SELECT rowid AS rid, * FROM companies) c2 ON c2.rid=t2.rid WHERE t2.target<>t2.rid GROUP BY 1) m
        ON m.target=k.rid"""
    )
    print(
        c.execute("SELECT source, count(*) FROM companies_dedup GROUP BY 1 ORDER BY 1").fetchall(),
        c.execute("SELECT count(*) FROM tgt WHERE target<>rid").fetchone()[0],
        "merged",
    )


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
LIST_COLUMNS = "name, sources, source_id, city, website, sector_code, size, status, listed_ticker"
UNCLASSIFIED = "Unclassified"


def slug(text: str) -> str:
    """'Wholesale & retail' -> 'wholesale_retail' (file-name safe)."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def lists(con: duckdb.DuckDBPyConnection | None = None, out: pathlib.Path | None = None) -> None:
    """Write lists/<ISO2>/<sector>.csv from ``companies_dedup`` (directory is recreated from scratch)."""
    c = con or db()
    out = out or HERE / "lists"
    shutil.rmtree(out, ignore_errors=True)
    groups = c.execute(
        f"""SELECT country, sector, count(*) FROM (SELECT country, coalesce(sector,'{UNCLASSIFIED}') sector
            FROM companies_dedup WHERE country IS NOT NULL) GROUP BY 1,2 ORDER BY 1,2"""
    ).fetchall()
    for country, sector, _n in groups:
        d = out / country
        d.mkdir(parents=True, exist_ok=True)
        c.execute(
            f"""COPY (SELECT {LIST_COLUMNS} FROM companies_dedup WHERE country=? AND coalesce(sector,'{UNCLASSIFIED}')=?
                ORDER BY name) TO '{d / (slug(sector) + ".csv")}' (HEADER)""",
            [country, sector],
        )
    print(len(groups), "files,", sum(g[2] for g in groups), "rows")


def export(con: duckdb.DuckDBPyConnection | None = None, out: pathlib.Path | None = None) -> None:
    """Write ``companies`` (rows with a sector and country) as Parquet partitioned by country and sector."""
    c = con or db()
    out = out or HERE / "by_sector_country"
    out.mkdir(exist_ok=True)
    c.execute(
        f"""COPY (SELECT * FROM companies WHERE sector IS NOT NULL AND country IS NOT NULL)
      TO '{out}' (FORMAT parquet, PARTITION_BY (country, sector), OVERWRITE_OR_IGNORE)"""
    )
    print(
        c.execute(
            "SELECT country, sector, count(*) n FROM companies WHERE sector IS NOT NULL GROUP BY 1,2 ORDER BY n DESC LIMIT 10"
        ).fetchall()
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
USAGE = __doc__.split("\n\n")[1]


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else None
    try:
        if cmd == "fetch" and len(argv) == 2:
            fetch(argv[1])
        elif cmd == "load" and len(argv) == 3:
            load(argv[1], argv[2])
        elif cmd == "export":
            export()
        elif cmd == "dedupe":
            dedupe()
        elif cmd == "lists":
            lists()
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except ValueError as exc:  # unknown fetch/load source
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
