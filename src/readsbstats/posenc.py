"""Scaled-integer codecs for the schema-v6 positions table.

SQLite stores small INTEGERs in 1-4 bytes vs a flat 8 for REAL; encoding
lat/lon at 1e-5° (~1 m — finer than ADS-B CPR itself) and gs/track/rssi at
0.1-unit precision halves the positions table (measured on the production
dump). Every encode/decode for positions columns lives here — do not
hand-roll `*100000` at call sites.
"""
from __future__ import annotations

# readsb addrtype values (tar1090/readsb "type" field). Codes are stored in
# positions.source — append new types, NEVER renumber existing codes.
# Must cover every type collector._is_adsb()/_is_mlat() classify:
# _is_adsb → ("adsb_icao", "adsb_icao_nt", "adsr_icao", "adsc"); _is_mlat → "mlat".
SOURCE_TO_CODE: dict[str, int] = {
    "adsb_icao": 0,
    "mlat": 1,
    "adsr_icao": 2,
    "mode_s": 3,
    "adsb_icao_nt": 4,
    "tisb_icao": 5,
    "tisb_trackfile": 6,
    "tisb_other": 7,
    "adsb_other": 8,
    "adsr_other": 9,
    "mode_ac": 10,
    "unknown": 11,
    "adsc": 12,
}
OTHER_CODE = 99
CODE_TO_SOURCE: dict[int, str] = {v: k for k, v in SOURCE_TO_CODE.items()}
CODE_TO_SOURCE[OTHER_CODE] = "other"


def enc5(v: float | None) -> int | None:
    """Degrees → int(deg × 1e5).

    Note: Python round() uses banker's rounding (half-to-even); the migration
    SQL uses ROUND() (half-away-from-zero). Exact-half inputs may differ by
    1 LSB (≤1e-5° / ~1 m), which is cosmetically irrelevant for ADS-B data.
    """
    return None if v is None else round(v * 100000)


def dec5(i: int | None) -> float | None:
    return None if i is None else i / 100000.0


def enc1(v: float | None) -> int | None:
    """0.1-unit codec for gs (kts), track (deg), rssi (dB).

    Note: Python round() uses banker's rounding (half-to-even); the migration
    SQL uses ROUND() (half-away-from-zero). Exact-half inputs may differ by
    1 LSB (≤0.1 unit), which is cosmetically irrelevant for ADS-B data.
    """
    return None if v is None else round(v * 10)


def dec1(i: int | None) -> float | None:
    return None if i is None else i / 10.0


def encode_source(s: str | None) -> int | None:
    if s is None:
        return None
    return SOURCE_TO_CODE.get(s, OTHER_CODE)


def decode_source(code: int | None) -> str | None:
    if code is None:
        return None
    return CODE_TO_SOURCE.get(code, "other")


def sql_source_case(col: str) -> str:
    """CASE expression translating a legacy source_type TEXT column to its
    code — used by the v5→v6 migration. NULL stays NULL."""
    whens = " ".join(
        f"WHEN '{name}' THEN {code}" for name, code in SOURCE_TO_CODE.items()
    )
    return (
        f"CASE WHEN {col} IS NULL THEN NULL "
        f"ELSE CASE {col} {whens} ELSE {OTHER_CODE} END END"
    )
