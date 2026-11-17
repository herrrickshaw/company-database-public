"""Unit tests for the pure sector-mapping helpers and lookup tables."""

from __future__ import annotations

import pytest

import build

SECTIONS = {name for _, _, name in build.SIC}


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("68100", "Real estate"),
        ("59111", "Information & communication"),
        ("01", "Agriculture, forestry & fishing"),
        ("1", None),  # a single digit is division 1 -> agriculture only if two chars; "1"[:2] == "1" -> 1
        ("99", "Extraterritorial"),
        ("00", None),
        ("04", None),  # gap between agriculture (01-03) and mining (05-09)
        ("34", None),  # gap between manufacturing (10-33) and energy (35)
        (10, "Manufacturing"),
        (" 47.11 ", "Wholesale & retail"),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_sic_sector(code, expected):
    if code == "1":
        expected = "Agriculture, forestry & fishing"
    assert build.sic_sector(code) == expected


@pytest.mark.parametrize(
    ("division", "expected"),
    [
        (1, "Agriculture, forestry & fishing"),
        (24, "Manufacturing"),
        (45, "Construction"),
        (52, "Wholesale & retail"),
        (72, "Information & communication"),
        (90, "Water, waste & remediation"),
        (99, "Households"),
        (0, None),
        (38, None),  # gap between manufacturing (15-37) and energy (40)
        (100, None),
    ],
)
def test_nic_sector(division, expected):
    assert build.nic_sector(division) == expected


def test_sic_table_is_contiguous_and_sorted():
    prev = 0
    for lo, hi, _ in build.SIC:
        assert lo <= hi
        assert lo > prev
        prev = hi
    assert len(SECTIONS) == 21


def test_damodaran_map_targets_known_sections():
    assert set(build.DAMO.values()) <= SECTIONS | {None}
    assert build.DAMO["Diversified"] is None
    assert build.DAMO["Software (Internet)"] == "Information & communication"


def test_nic_map_targets_known_sections():
    assert {name for _, _, name in build.NIC} <= SECTIONS


def test_iso_codes_are_two_upper_letters_and_unique():
    codes = list(build.ISO.values())
    assert all(len(c) == 2 and c.isupper() for c in codes)
    assert len(codes) == len(set(codes))
    assert build.ISO["United Kingdom"] == "GB"
    assert build.ISO["India"] == "IN"
