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


# ---------------------------------------------------------------------------
# Constants sanity check — guards against an accidental re-definition that
# would silently widen / narrow the int bound.
# ---------------------------------------------------------------------------

def test_sqlite_int_bounds_match_64bit_signed():
    assert SQLITE_INT_MAX == 2**63 - 1
    assert SQLITE_INT_MIN == -(2**63)
