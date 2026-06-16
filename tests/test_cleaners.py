"""Tests for src/readsbstats/cleaners.py — shared input-normalization helpers
used by collector (source_type), metrics_collector (scalar values), and
adsbx_enricher (registration/type/desc strings).

Audit 2026-05-31 — PY-3, PY-4, PY-10 — moves three near-identical
isinstance + strip + length-bound patterns into one tested module.
"""
from __future__ import annotations

import math

import pytest

from readsbstats.cleaners import (
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
    clean_short_text,
    coerce_metric_scalar,
    valid_icao_code,
    valid_icao_hex,
    valid_lat,
    valid_lon,
)


# ---------------------------------------------------------------------------
# clean_short_text — bounded-string coercion
# ---------------------------------------------------------------------------

class TestCleanShortText:
    def test_strips_and_returns_valid_string(self):
        assert clean_short_text("  hello  ", 32) == "hello"

    def test_truncates_when_over_limit(self):
        assert clean_short_text("a" * 100, 32) == "a" * 32

    def test_returns_none_for_empty_string_after_strip(self):
        assert clean_short_text("   ", 32) is None
        assert clean_short_text("", 32) is None

    def test_returns_none_for_non_string_types(self):
        assert clean_short_text(None, 32) is None
        assert clean_short_text(123, 32) is None
        assert clean_short_text(1.5, 32) is None
        assert clean_short_text(True, 32) is None
        assert clean_short_text({"k": "v"}, 32) is None
        assert clean_short_text(["a", "b"], 32) is None
        assert clean_short_text(b"bytes", 32) is None

    def test_preserves_inner_whitespace(self):
        assert clean_short_text(" foo bar ", 32) == "foo bar"

    def test_truncation_happens_after_strip(self):
        # Whitespace counts against the strip first; the slice is on the
        # already-stripped value. "   AB" with limit=1 → "A", not "" (since
        # the strip removes the leading spaces before slicing).
        assert clean_short_text("   AB", 1) == "A"

    # --- SEC-2: control-character stripping ---------------------------------

    def test_strips_embedded_nul_control_char(self):
        # An embedded NUL (and other C0/C1 control chars) must not survive
        # into the DB/UI/logs.
        assert clean_short_text("AB\x00CD", 32) == "ABCD"

    def test_strips_assorted_control_chars(self):
        # ESC (0x1b), backspace (0x08), DEL (0x7f), and a C1 char (0x9b).
        assert clean_short_text("a\x1bb\x08c\x7fd\x9be", 32) == "abcde"

    def test_preserves_tab_newline_carriage_return(self):
        # clean_short_text also cleans multi-line VDL2/ACARS bodies; \t \n \r
        # are legitimate content and MUST be preserved.
        assert clean_short_text("line1\nline2", 32) == "line1\nline2"
        assert clean_short_text("col1\tcol2", 32) == "col1\tcol2"
        assert clean_short_text("a\r\nb", 32) == "a\r\nb"

    def test_all_control_input_returns_none(self):
        # Stripping control chars can empty the string; the empty-check must
        # run on the post-control-strip result, so this is None (not "").
        assert clean_short_text("\x00\x07", 8) is None
        assert clean_short_text("\x00\x00", 32) is None

    def test_control_strip_then_truncate_order(self):
        # Control chars are removed before the length cap is applied.
        assert clean_short_text("A\x00B\x00C\x00D", 3) == "ABC"

    def test_newline_only_input_is_not_stripped_to_none(self):
        # A leading/trailing newline is whitespace, so .strip() removes it
        # first; a pure-newline string strips to empty → None.
        assert clean_short_text("\n\n", 32) is None


# ---------------------------------------------------------------------------
# coerce_metric_scalar — SQLite-binding-safe numeric coercion
# ---------------------------------------------------------------------------

class TestCoerceMetricScalar:
    def test_passes_through_finite_int(self):
        assert coerce_metric_scalar(0) == 0
        assert coerce_metric_scalar(42) == 42
        assert coerce_metric_scalar(-7) == -7

    def test_passes_through_finite_float(self):
        assert coerce_metric_scalar(0.0) == 0.0
        assert coerce_metric_scalar(3.14) == 3.14
        assert coerce_metric_scalar(-2.718) == -2.718

    def test_rejects_non_finite_float(self):
        assert coerce_metric_scalar(float("inf")) is None
        assert coerce_metric_scalar(float("-inf")) is None
        assert coerce_metric_scalar(float("nan")) is None

    def test_rejects_oversized_int(self):
        # SQLite's INTEGER column is a signed 64-bit value.
        assert coerce_metric_scalar(SQLITE_INT_MAX) == SQLITE_INT_MAX
        assert coerce_metric_scalar(SQLITE_INT_MIN) == SQLITE_INT_MIN
        assert coerce_metric_scalar(SQLITE_INT_MAX + 1) is None
        assert coerce_metric_scalar(SQLITE_INT_MIN - 1) is None
        assert coerce_metric_scalar(2**128) is None

    def test_rejects_bool_explicitly(self):
        # bool is an int subclass in Python; storing True/False as 1/0 would
        # silently mask a schema-drift bug where a metric field flipped to a
        # boolean upstream. Treat as malformed.
        assert coerce_metric_scalar(True) is None
        assert coerce_metric_scalar(False) is None

    def test_rejects_none(self):
        assert coerce_metric_scalar(None) is None

    def test_rejects_containers(self):
        assert coerce_metric_scalar({"k": 1}) is None
        assert coerce_metric_scalar([1, 2, 3]) is None
        assert coerce_metric_scalar((1, 2)) is None

    def test_rejects_strings(self):
        # A string-shaped metric value is upstream schema drift; the metrics
        # schema is numeric-only.
        assert coerce_metric_scalar("42") is None
        assert coerce_metric_scalar("") is None
        # "NaN"/"inf" as TEXT must stay rejected — never coerced to float nan/inf
        # (which the float branch would then drop, but the string must not even
        # reach a float() parse).
        assert coerce_metric_scalar("NaN") is None
        assert coerce_metric_scalar("inf") is None


# ---------------------------------------------------------------------------
# Constants sanity check — guards against an accidental re-definition that
# would silently widen / narrow the int bound.
# ---------------------------------------------------------------------------

def test_sqlite_int_bounds_match_64bit_signed():
    assert SQLITE_INT_MAX == 2**63 - 1
    assert SQLITE_INT_MIN == -(2**63)


# ---------------------------------------------------------------------------
# valid_lat / valid_lon — coordinate validators
# ---------------------------------------------------------------------------

class TestValidLat:
    def test_accepts_in_range_floats(self):
        assert valid_lat(0.0) == 0.0
        assert valid_lat(52.23) == 52.23
        assert valid_lat(-33.87) == -33.87
        assert valid_lat(90.0) == 90.0
        assert valid_lat(-90.0) == -90.0

    def test_accepts_in_range_int(self):
        assert valid_lat(45) == 45.0

    def test_accepts_numeric_strings(self):
        assert valid_lat("52.23") == 52.23
        assert valid_lat("-90") == -90.0

    def test_rejects_out_of_range(self):
        assert valid_lat(100) is None
        assert valid_lat(-200) is None
        assert valid_lat(90.0001) is None
        assert valid_lat(-90.0001) is None

    def test_rejects_non_numeric_string(self):
        assert valid_lat("abc") is None
        assert valid_lat("") is None

    def test_rejects_none(self):
        assert valid_lat(None) is None

    def test_rejects_bool(self):
        # bool is an int subclass; True would coerce to 1.0 (in range) without
        # the explicit guard. Treat as malformed.
        assert valid_lat(True) is None
        assert valid_lat(False) is None

    def test_rejects_non_finite(self):
        assert valid_lat(float("nan")) is None
        assert valid_lat(float("inf")) is None
        assert valid_lat(float("-inf")) is None


class TestValidLon:
    def test_accepts_in_range_floats(self):
        assert valid_lon(0.0) == 0.0
        assert valid_lon(21.01) == 21.01
        assert valid_lon(-151.21) == -151.21
        assert valid_lon(180.0) == 180.0
        assert valid_lon(-180.0) == -180.0

    def test_accepts_wider_range_than_lat(self):
        # 150 is a valid longitude but an invalid latitude.
        assert valid_lon(150.0) == 150.0
        assert valid_lat(150.0) is None

    def test_accepts_numeric_strings(self):
        assert valid_lon("21.01") == 21.01
        assert valid_lon("-180") == -180.0

    def test_rejects_out_of_range(self):
        assert valid_lon(200) is None
        assert valid_lon(-200) is None
        assert valid_lon(180.0001) is None
        assert valid_lon(-180.0001) is None

    def test_rejects_non_numeric_string(self):
        assert valid_lon("abc") is None
        assert valid_lon("") is None

    def test_rejects_none(self):
        assert valid_lon(None) is None

    def test_rejects_bool(self):
        assert valid_lon(True) is None
        assert valid_lon(False) is None

    def test_rejects_non_finite(self):
        assert valid_lon(float("nan")) is None
        assert valid_lon(float("inf")) is None
        assert valid_lon(float("-inf")) is None


# ---------------------------------------------------------------------------
# valid_icao_hex — 24-bit Mode-S address
# ---------------------------------------------------------------------------

class TestValidIcaoHex:
    def test_accepts_six_hex_digits_lowercased(self):
        assert valid_icao_hex("4CA8D3") == "4ca8d3"
        assert valid_icao_hex("4ca8d3") == "4ca8d3"
        assert valid_icao_hex("000000") == "000000"
        assert valid_icao_hex("ffffff") == "ffffff"

    def test_strips_surrounding_whitespace(self):
        assert valid_icao_hex("  4ca8d3  ") == "4ca8d3"

    def test_rejects_non_hex_chars(self):
        # 'o' and 'k' are not hex digits.
        assert valid_icao_hex("48ok01") is None

    def test_rejects_over_length_without_truncating(self):
        # CRITICAL: over-length must be REJECTED, not truncated to a valid
        # 6-char prefix.
        assert valid_icao_hex("4ca8d3xyz") is None
        assert valid_icao_hex("4ca8d300") is None

    def test_rejects_under_length(self):
        assert valid_icao_hex("4ca8d") is None
        assert valid_icao_hex("") is None

    def test_rejects_non_string(self):
        assert valid_icao_hex(None) is None
        assert valid_icao_hex(0x4CA8D3) is None
        assert valid_icao_hex(["4ca8d3"]) is None


# ---------------------------------------------------------------------------
# valid_icao_code — ICAO (4) / IATA (3) airport codes
# ---------------------------------------------------------------------------

class TestValidIcaoCode:
    def test_accepts_four_char_icao_uppercased(self):
        assert valid_icao_code("EPWA") == "EPWA"
        assert valid_icao_code("epwa") == "EPWA"
        assert valid_icao_code("KJFK", 4) == "KJFK"

    def test_strips_then_validates(self):
        assert valid_icao_code("epwa ") == "EPWA"
        assert valid_icao_code("  EPWA  ", 4) == "EPWA"

    def test_accepts_alphanumeric(self):
        # ICAO codes can contain digits.
        assert valid_icao_code("EP01") == "EP01"

    def test_rejects_wrong_length(self):
        assert valid_icao_code("EP") is None
        assert valid_icao_code("EPWAX") is None
        assert valid_icao_code("EPWA", 3) is None

    def test_rejects_non_alphanumeric(self):
        assert valid_icao_code("EPW@") is None
        assert valid_icao_code("EP WA") is None
        assert valid_icao_code("EP-A") is None

    def test_three_char_iata_mode(self):
        assert valid_icao_code("WAW", 3) == "WAW"
        assert valid_icao_code("waw", 3) == "WAW"
        assert valid_icao_code("WA", 3) is None
        assert valid_icao_code("WAWX", 3) is None

    def test_rejects_non_string(self):
        assert valid_icao_code(None) is None
        assert valid_icao_code(1234) is None
        assert valid_icao_code(["EPWA"]) is None
