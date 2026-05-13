"""Tests for icao_ranges.py — icao_to_country lookups + anonymous-hex detection."""

import sqlite3

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

    def test_qatar_icao(self):
        # Qatar block: 0x06A000–0x06A3FF.  Added 2026-05-13 after auditing
        # the FLAG_ANONYMOUS feature surfaced 60+ Qatar Airways (A7-Bxx)
        # aircraft as false positives — the table was missing this allocation.
        assert icao_ranges.icao_to_country("06a066") == "Qatar"
        assert icao_ranges.icao_to_country("06a0bf") == "Qatar"
        assert icao_ranges.icao_to_country("06a3ff") == "Qatar"

    def test_qatar_not_anonymous(self):
        # Direct guard against regressing the Qatar gap.
        assert icao_ranges.is_anonymous_icao("06a066") is False
        assert icao_ranges.is_anonymous_icao("06a0bf") is False


class TestIsAnonymousIcao:
    # The real-world DD85CB sighting that motivated this feature — ADSBExchange
    # flags it as a non-ICAO hex; nothing in tar1090-db; we should agree.
    def test_dd85cb_is_anonymous(self):
        assert icao_ranges.is_anonymous_icao("dd85cb") is True

    def test_dd_range_is_anonymous(self):
        # ICAO leaves the 0xDC0000–0xDFFFFF region unassigned to any state.
        assert icao_ranges.is_anonymous_icao("dc0000") is True
        assert icao_ranges.is_anonymous_icao("dfffff") is True

    def test_f_range_is_anonymous(self):
        # 0xF00000+ is reserved for ICAO special / temporary schemes.
        assert icao_ranges.is_anonymous_icao("f00001") is True
        assert icao_ranges.is_anonymous_icao("ffffff") is True

    def test_gap_between_state_blocks_is_anonymous(self):
        # Uruguay ends at 0xE90FFF, Bolivia starts at 0xE94000 — 0xE91000 is a gap.
        assert icao_ranges.is_anonymous_icao("e91000") is True

    @pytest.mark.parametrize("hex_,country", [
        ("488001", "Poland"),
        ("4d20fb", "Malta"),
        ("471dba", "Hungary"),
        ("3c674a", "Germany"),
        ("a00001", "United States"),
        ("c00001", "Canada"),
        ("405000", "United Kingdom"),
        ("100001", "Russia"),
        ("4cb2a1", "Unknown"),     # gap inside the European 0x4C range
    ])
    def test_state_blocks_not_anonymous(self, hex_, country):
        # Anything in a real country block must NOT be flagged as anonymous,
        # even when the country lookup itself returns "Unknown" (we cross-check).
        is_anon = icao_ranges.is_anonymous_icao(hex_)
        if country == "Unknown":
            # 4cb2a1 falls between Cyprus (0x4C8000-0x4C83FF) and Ireland
            # (0x4CA000-0x4CAFFF) — a real gap, so it IS anonymous.
            assert is_anon is True
        else:
            assert is_anon is False, f"{hex_} ({country}) misclassified as anonymous"

    def test_uppercase_input_accepted(self):
        assert icao_ranges.is_anonymous_icao("DD85CB") is True
        assert icao_ranges.is_anonymous_icao("488001".upper()) is False

    def test_none_returns_false(self):
        # Don't noise-flag missing data — only intentional non-state addresses.
        assert icao_ranges.is_anonymous_icao(None) is False

    def test_empty_string_returns_false(self):
        assert icao_ranges.is_anonymous_icao("") is False

    def test_invalid_hex_returns_false(self):
        assert icao_ranges.is_anonymous_icao("GGGGGG") is False
        assert icao_ranges.is_anonymous_icao("xyz") is False

    def test_out_of_24bit_range_returns_false(self):
        # 0x1000000 is 25 bits — Mode S can't transmit it, so calling it
        # "anonymous" misrepresents the situation.  Treat as malformed.
        assert icao_ranges.is_anonymous_icao("1000000") is False

    def test_zero_address_is_anonymous(self):
        # 0x000000 is the Mode-S "no address" broadcast — not assigned to
        # any state, definitely worth flagging if a real receiver picks it up.
        assert icao_ranges.is_anonymous_icao("000000") is True


class TestAnonymousFlagSql:
    """SQL CASE expression mirrors is_anonymous_icao() exactly."""

    @pytest.fixture
    def conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE t (icao_hex TEXT)")
        yield c
        c.close()

    def test_sql_matches_python_for_known_cases(self, conn):
        # Mix of state-allocated, gap, top-D, top-F, and the motivating example
        sample = [
            "dd85cb", "dc0000", "dfffff", "f00001", "ffffff", "e91000",
            "000000",
            "488001", "4d20fb", "471dba", "3c674a", "a00001", "c00001",
            "405000", "100001",
        ]
        conn.executemany("INSERT INTO t VALUES (?)", [(h,) for h in sample])
        expr = icao_ranges.anonymous_flag_sql("icao_hex", 16)
        rows = conn.execute(f"SELECT icao_hex, ({expr}) AS bit FROM t").fetchall()
        for r in rows:
            sql_says_anon = (r["bit"] == 16)
            py_says_anon  = icao_ranges.is_anonymous_icao(r["icao_hex"])
            assert sql_says_anon == py_says_anon, (
                f"SQL/Python disagree on {r['icao_hex']}: "
                f"sql={sql_says_anon} py={py_says_anon}"
            )

    def test_sql_or_merges_into_existing_flags(self, conn):
        # Simulate the production pattern: existing flags column OR'd with
        # the anonymous CASE.  An aircraft with flags=1 (military) and a
        # non-state hex must end up with flags=17 (military + anon).
        conn.execute("ALTER TABLE t ADD COLUMN flags INTEGER")
        conn.executemany(
            "INSERT INTO t VALUES (?, ?)",
            [("dd85cb", 0), ("dd85cb", 1), ("488001", 1), ("488001", 0)],
        )
        expr = icao_ranges.anonymous_flag_sql("icao_hex", 16)
        rows = conn.execute(
            f"SELECT icao_hex, flags, (COALESCE(flags,0) | {expr}) AS merged FROM t"
        ).fetchall()
        merged = {(r["icao_hex"], r["flags"]): r["merged"] for r in rows}
        assert merged[("dd85cb", 0)] == 16    # anon only
        assert merged[("dd85cb", 1)] == 17    # military + anon
        assert merged[("488001", 1)] == 1     # military only (Polish block)
        assert merged[("488001", 0)] == 0     # plain civilian

    def test_custom_column_name(self):
        # The helper must accept qualified column names like 'f.icao_hex'.
        expr = icao_ranges.anonymous_flag_sql("f.icao_hex", 16)
        assert "f.icao_hex" in expr
        assert "ELSE 16 END" in expr

    def test_custom_flag_value(self):
        # If we ever bump FLAG_ANONYMOUS, the helper picks up the new value.
        expr = icao_ranges.anonymous_flag_sql("icao_hex", 32)
        assert "ELSE 32 END" in expr
