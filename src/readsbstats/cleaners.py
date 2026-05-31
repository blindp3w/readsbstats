"""Shared input-normalization helpers for untrusted upstream data.

Three modules used to repeat near-identical isinstance + strip + cap
patterns when accepting values from external feeds (readsb's
``aircraft.json``, ``stats.json``, the airplanes.live area API):

* ``collector._cap`` for feed strings on flights/positions rows
* ``adsbx_enricher._parse_area_response`` for r/t/desc on adsbx_overrides
* ``metrics_collector._parse_stats`` for numeric scalars on receiver_stats

This module centralises the two non-trivial ones:

* :func:`clean_short_text` — bounded-string coercion: returns ``None`` for
  any non-string, empty-after-strip, or oversized value; otherwise the
  stripped value truncated to ``limit`` characters.
* :func:`coerce_metric_scalar` — numeric coercion safe for SQLite binding:
  returns ``None`` for ``bool``, ``None``, dict/list/other containers,
  non-finite floats, or ints outside SQLite's signed 64-bit range.

Audit 2026-05-31 — PY-3 / PY-4 / PY-10.
"""
from __future__ import annotations

import math

SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1


def clean_short_text(value: object, limit: int) -> str | None:
    """Return *value* as a stripped, length-capped string, or ``None`` for
    any non-string / empty / unusable input.

    A non-string value (dict, list, int, ``None``) becomes ``None`` so a
    malformed feed field never reaches a SQLite TEXT binding as a non-str
    type (which would raise and roll back the surrounding write).
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def coerce_metric_scalar(value: object) -> int | float | None:
    """Return *value* if it is a finite, SQLite-bind-safe numeric scalar;
    otherwise ``None``.

    Rejects (returns ``None`` for):

    * ``bool`` — Python booleans are an ``int`` subclass; silently storing
      ``True``/``False`` as ``1``/``0`` would mask an upstream schema flip
      from numeric to boolean. We treat that as malformed.
    * ``None`` — already absent.
    * dict / list / tuple / other containers — schema drift.
    * Non-finite floats (``inf``, ``-inf``, ``nan``) — SQLite stores these
      as text in some configurations and they break ``AVG``/``SUM``.
    * Integers outside SQLite's signed 64-bit range — binding raises
      ``OverflowError`` and aborts the whole row.
    * Strings — the receiver_stats schema is numeric-only; a string value
      means the upstream JSON shape has changed.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if SQLITE_INT_MIN <= value <= SQLITE_INT_MAX else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None
