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
  stripped value, with C0/C1 control characters removed, truncated to
  ``limit`` characters.
* :func:`coerce_metric_scalar` — numeric coercion safe for SQLite binding:
  returns ``None`` for ``bool``, ``None``, dict/list/other containers,
  non-finite floats, or ints outside SQLite's signed 64-bit range.

It is also the canonical home for coordinate/identifier validators used at
the boundary by config / route_enricher / vdl2-normalize / watchlist:
:func:`valid_lat`, :func:`valid_lon`, :func:`valid_icao_hex`,
:func:`valid_icao_code`.

Audit 2026-05-31 — PY-3 / PY-4 / PY-10. Audit 2026-06-06 — SEC-2 / WS-2a.
"""
from __future__ import annotations

import math
import re

SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1

# C0 controls (minus \t \n \r), DEL, and C1 controls. Stripped from any
# short-text field so NUL/ESC/etc. never reach the DB/UI/logs (SEC-2).
# \t (0x09), \n (0x0a), \r (0x0d) are deliberately PRESERVED — clean_short_text
# also cleans multi-line VDL2/ACARS message bodies (vdl2/normalize.py), where
# tabs and newlines are legitimate content.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_short_text(value: object, limit: int) -> str | None:
    r"""Return *value* as a stripped, length-capped string, or ``None`` for
    any non-string / empty / unusable input.

    A non-string value (dict, list, int, ``None``) becomes ``None`` so a
    malformed feed field never reaches a SQLite TEXT binding as a non-str
    type (which would raise and roll back the surrounding write).

    Embedded C0/C1 control characters (NUL, ESC, DEL, …) are removed after
    stripping so they never reach the DB/UI/logs (SEC-2). ``\t``/``\n``/``\r``
    are preserved — this also normalises multi-line VDL2/ACARS bodies. If
    removing control characters empties the string, the result is ``None``.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    value = _CONTROL_RE.sub("", value)
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


def valid_lat(value: object) -> float | None:
    """Latitude coerced to float in [-90, 90], else None.

    Accepts int/float/numeric-str; rejects bool/NaN/inf/out-of-range/None.
    ``bool`` is rejected explicitly (it is an ``int`` subclass, so ``True``
    would otherwise coerce to the in-range ``1.0``).
    """
    if isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and -90.0 <= f <= 90.0 else None


def valid_lon(value: object) -> float | None:
    """Longitude coerced to float in [-180, 180], else None.

    Same coercion/rejection rules as :func:`valid_lat`, with the wider bound.
    """
    if isinstance(value, bool):
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and -180.0 <= f <= 180.0 else None


def valid_icao_hex(value: object) -> str | None:
    """A 24-bit Mode-S hex address: exactly 6 hex digits, lowercased. Else None.

    Must REJECT (not truncate) over-length input, so this does NOT route
    through :func:`clean_short_text`'s length cap.
    """
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    return s if len(s) == 6 and all(c in "0123456789abcdef" for c in s) else None


def valid_icao_code(value: object, n: int = 4) -> str | None:
    """An ICAO (``n=4``) / IATA (``n=3``) airport code: exactly *n* uppercase
    ASCII alphanumerics. Else None.
    """
    if not isinstance(value, str):
        return None
    s = value.strip().upper()
    return s if re.fullmatch(rf"[A-Z0-9]{{{n}}}", s) else None
