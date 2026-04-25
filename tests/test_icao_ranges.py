"""Tests for icao_ranges.py — icao_to_country lookups."""

import pytest
from readsbstats import icao_ranges


class TestIcaoToCountry:
    def test_polish_icao(self):
        # Poland block: 0x488000–0x48FFFF
        assert icao_ranges.icao_to_country("488001") == "Poland"

    def test_us_icao(self):
        # United States block: 0xA00000–0xAFFFFF
        assert icao_ranges.icao_to_country("a00001") == "United States"

    def test_german_icao(self):
        # Germany block: 0x3C0000–0x3FFFFF
        assert icao_ranges.icao_to_country("3c0001") == "Germany"

    def test_russian_icao(self):
        # Russia block: 0x100000–0x1FFFFF
        assert icao_ranges.icao_to_country("100001") == "Russia"

    def test_uk_icao(self):
        # UK large block: 0x400000–0x43FFFF
        assert icao_ranges.icao_to_country("405000") == "United Kingdom"

    def test_uppercase_hex_accepted(self):
        # Function should handle uppercase
        assert icao_ranges.icao_to_country("488001") == icao_ranges.icao_to_country("488001".upper())

    def test_unknown_range_returns_unknown(self):
        # 0x000001 falls outside all defined blocks
        assert icao_ranges.icao_to_country("000001") == "Unknown"

    def test_invalid_hex_returns_unknown(self):
        assert icao_ranges.icao_to_country("GGGGGG") == "Unknown"

    def test_none_returns_unknown(self):
        assert icao_ranges.icao_to_country(None) == "Unknown"

    def test_empty_string_returns_unknown(self):
        assert icao_ranges.icao_to_country("") == "Unknown"

    def test_french_icao(self):
        # France block: 0x380000–0x3BFFFF
        assert icao_ranges.icao_to_country("380001") == "France"

    def test_brazilian_icao(self):
        # Brazil block: 0xE40000–0xE7FFFF
        assert icao_ranges.icao_to_country("e40001") == "Brazil"
