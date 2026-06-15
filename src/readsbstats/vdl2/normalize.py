"""Decoder-agnostic normalization: raw decoder JSON -> one internal record.

vdlm2dec is the working decoder (Airspy Mini). dumpvdl2 is the documented
future swap; its JSON dialect differs (nested ``vdl2.avlc.acars.*``), so the
swap is a config flip (``RSBS_VDL2_DECODER``) plus completing the dumpvdl2
mapping here — no changes anywhere else in the pipeline.

Every normalizer returns a dict keyed by :data:`db.COLUMNS` (missing keys
default to ``None`` at insert time), or ``None`` to drop the datagram. Short
identifier fields go through :func:`cleaners.clean_short_text` (the project's
untrusted-input coercion) so a malformed field can never reach a TEXT binding
as a non-string. The Mode-S address goes through :func:`cleaners.valid_icao_hex`
(6-hex or ``None``) and coordinates through :func:`cleaners.valid_lat` /
:func:`cleaners.valid_lon` (range-checked or ``None``) — F05.
"""
from __future__ import annotations

import json
import time

from .. import config
from ..cleaners import (
    clean_short_text,
    coerce_metric_scalar,
    valid_icao_hex,
    valid_lat,
    valid_lon,
)

_SHORT = 64        # identifier-field cap (reg/flight/label/etc.)


def _ts(value: object) -> int:
    """Coerce an epoch timestamp (vdlm2dec emits a float) to int seconds.
    Falls back to now() when missing/unparseable, and clamps implausible
    *future* values (decoder/clock glitch) to now so a single bad ts can't sort
    to the top of the feed forever. Old timestamps are left as-is (legit backfill)."""
    now = int(time.time())
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, (int, float)):
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(float(value))
        except ValueError:
            parsed = None
    if parsed is None:
        return now
    if parsed > now + 86400:        # > 1 day in the future → clock/decoder glitch
        return now
    return parsed


def _label(value: object) -> str | None:
    """ACARS labels are uppercase 2-char codes; normalize case so filters match."""
    lbl = clean_short_text(value, _SHORT)
    return lbl.upper() if lbl else None


def _reg(value: object) -> str | None:
    """Aircraft registration. dumpvdl2's ACARS reg field arrives left-padded with a
    '.' (e.g. '.TC-NCU'); strip leading dots/spaces so it matches vdlm2dec's clean
    tail and core flights.registration ('TC-NCU'). No-op for already-clean regs; no
    registration legitimately starts with a dot or space, so '.9H-WAU' → '9H-WAU'
    (the digit is kept) and '.' → None."""
    reg = clean_short_text(value, _SHORT)
    if not reg:
        return None
    return reg.lstrip(". ") or None


def _raw_json(raw: dict) -> str:
    """Serialize the full decoder datagram, capped (per-row growth defense)."""
    return json.dumps(raw, separators=(",", ":"), default=str)[: config.VDL2_RAW_MAX]


def _num(value: object) -> int | float | None:
    """Coerce to a bind-safe number, tolerating numeric *strings* (some decoder
    dialects quote freq/lat/lon). coerce_metric_scalar alone rejects strings."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return coerce_metric_scalar(value)


def _int_or_none(value: object) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def _float_or_none(value: object) -> float | None:
    n = _num(value)
    return float(n) if n is not None else None


def _has_content(rec: dict) -> bool:
    """Drop completely empty records (no identity, no body)."""
    return any(rec.get(k) for k in ("icao_hex", "registration", "flight", "label", "body"))


def _normalize_vdlm2dec(raw: dict) -> dict | None:
    app = raw.get("app")
    app_name = app_ver = None
    if isinstance(app, dict):
        app_name = clean_short_text(app.get("name"), _SHORT)
        app_ver = clean_short_text(app.get("ver") or app.get("version"), _SHORT)
    rec = {
        "ts":           _ts(raw.get("timestamp")),
        # valid_icao_hex enforces 6-hex + lowercases to match core
        # flights.icao_hex casing; rejects malformed identifiers outright.
        "icao_hex":     valid_icao_hex(raw.get("hex") or raw.get("icao")),
        "registration": _reg(raw.get("tail")),
        "flight":       clean_short_text(raw.get("flight"), _SHORT),
        "label":        _label(raw.get("label")),
        "mode":         clean_short_text(raw.get("mode"), _SHORT),
        "block_id":     clean_short_text(raw.get("block_id"), _SHORT),
        "ack":          clean_short_text(raw.get("ack"), _SHORT),
        "msgno":        clean_short_text(raw.get("msgno"), _SHORT),
        "freq":         _float_or_none(raw.get("freq")),   # vdlm2dec emits MHz
        "station_id":   clean_short_text(raw.get("station_id"), _SHORT),
        "toaddr":       clean_short_text(raw.get("toaddr"), _SHORT),
        "dsta":         clean_short_text(raw.get("dsta"), _SHORT),
        "lat":          valid_lat(raw.get("lat")),
        "lon":          valid_lon(raw.get("lon")),
        "alt":          _int_or_none(raw.get("alt")),
        "epu":          _float_or_none(raw.get("epu")),
        "app_name":     app_name,
        "app_ver":      app_ver,
        "body":         clean_short_text(raw.get("text"), config.VDL2_BODY_MAX),
        "raw":          _raw_json(raw),
        "decoder":      "vdlm2dec",
    }
    return rec if _has_content(rec) else None


def _normalize_dumpvdl2(raw: dict) -> dict | None:
    """Map a dumpvdl2 frame. The ACARS field map + ``vdl2.station`` key below are
    verified against real ``--output decoded:json`` captures (2026-06-14/15 overnight
    run off the Airspy Mini via the iq_tool resample pipe). dumpvdl2 nests ACARS under
    ``vdl2.avlc.acars`` (address in ``vdl2.avlc.src.addr``), reports ``freq`` in **Hz**
    (converted to MHz below), and emits the ``--station-id`` value as ``vdl2.station``.
    TODO: ATN/OSI frames (``vdl2.avlc.x25`` — CPDLC/ADS-C/MIAM, ~72% of frames) are not
    yet extracted; they currently fall through to bare ``icao_hex`` rows (see #2).
    """
    vdl2 = raw.get("vdl2") if isinstance(raw.get("vdl2"), dict) else {}
    avlc = vdl2.get("avlc") if isinstance(vdl2.get("avlc"), dict) else {}
    src = avlc.get("src") if isinstance(avlc.get("src"), dict) else {}
    acars = avlc.get("acars") if isinstance(avlc.get("acars"), dict) else {}
    freq_hz = _float_or_none(vdl2.get("freq"))
    rec = {
        "ts":           _ts((vdl2.get("t") or {}).get("sec") if isinstance(vdl2.get("t"), dict) else raw.get("timestamp")),
        # valid_icao_hex enforces 6-hex + lowercases; rejects malformed addrs.
        "icao_hex":     valid_icao_hex(src.get("addr")),
        "registration": _reg(acars.get("reg")),
        "flight":       clean_short_text(acars.get("flight"), _SHORT),
        "label":        _label(acars.get("label")),
        "mode":         clean_short_text(acars.get("mode"), _SHORT),
        "block_id":     clean_short_text(acars.get("blk_id"), _SHORT),
        "ack":          clean_short_text(acars.get("ack"), _SHORT),
        "msgno":        clean_short_text(acars.get("msg_num"), _SHORT),
        "freq":         freq_hz / 1e6 if freq_hz else None,   # Hz → MHz (0/None → None)
        "station_id":   clean_short_text(vdl2.get("station"), _SHORT),  # dumpvdl2: vdl2.station
        "toaddr":       clean_short_text((avlc.get("dst") or {}).get("addr") if isinstance(avlc.get("dst"), dict) else None, _SHORT),
        "dsta":         None,
        "lat":          None,
        "lon":          None,
        "alt":          None,
        "epu":          None,
        "app_name":     None,
        "app_ver":      None,
        "body":         clean_short_text(acars.get("msg_text"), config.VDL2_BODY_MAX),
        "raw":          _raw_json(raw),
        "decoder":      "dumpvdl2",
    }
    return rec if _has_content(rec) else None


def normalize(raw: object, decoder: str | None = None) -> dict | None:
    """Dispatch to the decoder-specific normalizer. Returns None for non-dict
    input or an empty record (so the caller can drop it)."""
    if not isinstance(raw, dict):
        return None
    if (decoder or config.VDL2_DECODER) == "dumpvdl2":
        return _normalize_dumpvdl2(raw)
    return _normalize_vdlm2dec(raw)
