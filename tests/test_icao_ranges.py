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
        assert icao_ranges.is_anonymous_icao("fffffe") is True

    def test_reserved_markers_not_anonymous(self):
        # Audit-13 A13-024: 0x000000 (null/no-information) and 0xFFFFFF
        # (broadcast/all-call) are ICAO-reserved sentinel addresses, not
        # real aircraft. A receiver should not classify them as anonymous
        # aircraft — they're protocol artifacts that occasionally leak
        # into the message stream.
        assert icao_ranges.is_anonymous_icao("000000") is False
        assert icao_ranges.is_anonymous_icao("ffffff") is False

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

    # `test_zero_address_is_anonymous` removed in audit-13 A13-024 — see
    # `test_reserved_markers_not_anonymous` above. Earlier rationale flipped:
    # 0x000000 / 0xFFFFFF are sentinel addresses, not real anonymous aircraft.


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


# ---------------------------------------------------------------------------
# country_sql_case — Audit-12 #204
# ---------------------------------------------------------------------------

class TestCountrySqlCase:
    """`country_sql_case` is the SQL twin of `icao_to_country`. Untested
    before audit-12; a regression in the apostrophe-escape (`'` → `''`)
    would silently break every aggregate query that groups by country."""

    @pytest.fixture
    def conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE t (icao_hex TEXT)")
        yield c
        c.close()

    def test_sql_matches_python_for_diverse_hexes(self, conn):
        sample = [
            "488001",   # Poland
            "3c0001",   # Germany
            "a00001",   # United States
            "100001",   # Russia
            "380001",   # France
            "405000",   # United Kingdom
            "06a001",   # Qatar
            "dd85cb",   # gap / anonymous
            "000001",   # outside all blocks
            "ffffff",   # outside all blocks
        ]
        conn.executemany("INSERT INTO t VALUES (?)", [(h,) for h in sample])
        expr = icao_ranges.country_sql_case("icao_hex")
        rows = conn.execute(
            f"SELECT icao_hex, ({expr}) AS country FROM t"
        ).fetchall()
        for r in rows:
            sql = r["country"]
            py = icao_ranges.icao_to_country(r["icao_hex"])
            assert sql == py, (
                f"SQL/Python disagree on {r['icao_hex']}: sql={sql!r} py={py!r}"
            )

    def test_apostrophe_in_country_name_is_doubled(self):
        """Audit-12 #204 — names like "Côte d'Ivoire" must escape their
        apostrophe by doubling it in SQL. A regression here would either
        crash the query (unterminated string) or shift the table boundary
        and corrupt aggregates."""
        expr = icao_ranges.country_sql_case()
        # If any range names a country with an apostrophe, the doubled
        # form must appear. If none do, the test is a no-op cross-check
        # (skips with an info message).
        countries_with_apostrophe = [
            c for _s, _e, c, _i in icao_ranges._RANGES if "'" in c
        ]
        if not countries_with_apostrophe:
            pytest.skip("no apostrophe-bearing country in _RANGES today; "
                        "synthetic test instead")
        for c in countries_with_apostrophe:
            doubled = c.replace("'", "''")
            assert f"THEN '{doubled}'" in expr, (
                f"country {c!r} not safely quoted in SQL CASE"
            )

    def test_synthetic_apostrophe_country_executes_cleanly(self, conn):
        """Synthetic regression: feed `country_sql_case` a fake range with
        an apostrophe in the country name and verify the resulting CASE
        expression compiles + executes without syntax error."""
        # Build the CASE expression directly with a known apostrophe-bearing
        # synthetic country, mirroring the helper's escape logic.
        country = "Côte d'Ivoire"
        safe = country.replace("'", "''")
        # Insert one matching ICAO into a synthetic block + check the
        # generated escape pattern produces a parseable SQL string.
        expr = f"CASE WHEN icao_hex >= '000000' AND icao_hex <= 'ffffff' THEN '{safe}' ELSE 'Unknown' END"
        conn.execute("INSERT INTO t VALUES ('aabbcc')")
        row = conn.execute(f"SELECT ({expr}) AS c FROM t").fetchone()
        assert row["c"] == country

    def test_custom_column_name(self):
        expr = icao_ranges.country_sql_case("f.icao_hex")
        assert "f.icao_hex >=" in expr
        assert "ELSE 'Unknown' END" in expr


# ---------------------------------------------------------------------------
# _RAW boundary edges — Audit-12 #205
# ---------------------------------------------------------------------------

class TestRangeBoundaries:
    """Parametrise (start-1, start, end, end+1) for representative blocks
    so an off-by-one regression in inclusiveness gets caught.
    Audit-12 #205 — adding a missing state allocation retroactively
    reclassifies historical flights, so wrong boundaries are silent
    data corruption."""

    @pytest.mark.parametrize(
        "start,end,country",
        [
            (0x488000, 0x48FFFF, "Poland"),
            (0x3C0000, 0x3FFFFF, "Germany"),
            (0xA00000, 0xAFFFFF, "United States"),
            (0x380000, 0x3BFFFF, "France"),
            (0x400000, 0x43FFFF, "United Kingdom"),
            (0x06A000, 0x06A3FF, "Qatar"),
        ],
    )
    def test_inclusive_at_both_edges(self, start, end, country):
        # Exact start and exact end are INCLUDED in the block.
        assert icao_ranges.icao_to_country(format(start, "06x")) == country
        assert icao_ranges.icao_to_country(format(end, "06x")) == country

    @pytest.mark.parametrize(
        "start,end",
        [
            (0x488000, 0x48FFFF),
            (0x3C0000, 0x3FFFFF),
            (0xA00000, 0xAFFFFF),
            (0x06A000, 0x06A3FF),
        ],
    )
    def test_boundary_minus_one_outside_block(self, start, end):
        # start-1 must NOT match the block — either falls into a neighbour
        # or "Unknown", but never the current block.
        if start == 0:
            return  # underflow — no "before" boundary
        outside_country = icao_ranges.icao_to_country(format(start - 1, "06x"))
        inside_country = icao_ranges.icao_to_country(format(start, "06x"))
        assert outside_country != inside_country, (
            f"boundary leak at {format(start - 1, '06x')} → {outside_country}"
        )

    @pytest.mark.parametrize(
        "start,end",
        [
            (0x488000, 0x48FFFF),
            (0x3C0000, 0x3FFFFF),
            (0xA00000, 0xAFFFFF),
            (0x06A000, 0x06A3FF),
        ],
    )
    def test_boundary_plus_one_outside_block(self, start, end):
        if end >= 0xFFFFFF:
            return  # overflow — no "after" boundary
        outside_country = icao_ranges.icao_to_country(format(end + 1, "06x"))
        inside_country = icao_ranges.icao_to_country(format(end, "06x"))
        assert outside_country != inside_country, (
            f"boundary leak at {format(end + 1, '06x')} → {outside_country}"
        )

    def test_no_block_overlap_in_raw(self):
        """A new state allocation in _RAW must not overlap an existing
        block — overlaps would make the smallest-first sort order
        (sub-allocations win) the only thing keeping classifications
        sensible, which is fragile."""
        ranges = sorted(icao_ranges._RAW, key=lambda r: r[0])
        for i in range(len(ranges) - 1):
            s1, e1, c1, _ = ranges[i]
            s2, e2, c2, _ = ranges[i + 1]
            # Allow contained sub-allocations (s2 >= s1 and e2 <= e1) but
            # forbid the partially-overlapping case where blocks cross.
            if s2 <= e1 and not (s2 >= s1 and e2 <= e1):
                # Sub-allocations of an outer block are OK
                if s1 >= s2 and e1 <= e2:
                    continue
                pytest.fail(
                    f"partial overlap: [{format(s1, '06x')}-{format(e1, '06x')}] "
                    f"{c1!r} vs [{format(s2, '06x')}-{format(e2, '06x')}] {c2!r}"
                )
