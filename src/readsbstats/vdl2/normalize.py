"""Decoder-agnostic normalization: raw decoder JSON -> one internal record.

vdlm2dec is the working decoder (Airspy Mini). dumpvdl2 is the documented
future swap; its JSON dialect differs (nested ``vdl2.avlc.acars.*``), so the
swap is a config flip (``RSBS_VDL2_DECODER``) plus completing the dumpvdl2
mapping here — no changes anywhere else in the pipeline.

Every normalizer returns a dict keyed by :data:`db.COLUMNS` (missing keys
default to ``None`` at insert time), or ``None`` to drop the datagram. Short
identifier fields go through :func:`cleaners.clean_short_text` (the project's
untrusted-input coercion) so a malformed field can never reach a TEXT binding
as a non-string.
"""
from __future__ import annotations

import json
import time

from .. import config
from ..cleaners import clean_short_text, coerce_metric_scalar

_SHORT = 64        # identifier-field cap (reg/flight/label/etc.)


def _ts(value: object) -> int:
    """Coerce an epoch timestamp (vdlm2dec emits a float) to int seconds.
    Falls back to now() when missing/unparseable so every row has a ts."""
    if isinstance(value, bool):
        value = None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            pass
    return int(time.time())


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
    hex_ = clean_short_text(raw.get("hex") or raw.get("icao"), _SHORT)
    app = raw.get("app")
    app_name = app_ver = None
    if isinstance(app, dict):
        app_name = clean_short_text(app.get("name"), _SHORT)
        app_ver = clean_short_text(app.get("ver") or app.get("version"), _SHORT)
    rec = {
        "ts":           _ts(raw.get("timestamp")),
        "icao_hex":     hex_.lower() if hex_ else None,   # match core flights.icao_hex casing
        "registration": clean_short_text(raw.get("tail"), _SHORT),
        "flight":       clean_short_text(raw.get("flight"), _SHORT),
        "label":        clean_short_text(raw.get("label"), _SHORT),
        "mode":         clean_short_text(raw.get("mode"), _SHORT),
        "block_id":     clean_short_text(raw.get("block_id"), _SHORT),
        "ack":          clean_short_text(raw.get("ack"), _SHORT),
        "msgno":        clean_short_text(raw.get("msgno"), _SHORT),
        "freq":         _float_or_none(raw.get("freq")),
        "station_id":   clean_short_text(raw.get("station_id"), _SHORT),
        "toaddr":       clean_short_text(raw.get("toaddr"), _SHORT),
        "dsta":         clean_short_text(raw.get("dsta"), _SHORT),
        "lat":          _float_or_none(raw.get("lat")),
        "lon":          _float_or_none(raw.get("lon")),
        "alt":          _int_or_none(raw.get("alt")),
        "epu":          _float_or_none(raw.get("epu")),
        "app_name":     app_name,
        "app_ver":      app_ver,
        "body":         clean_short_text(raw.get("text"), config.VDL2_BODY_MAX),
        "raw":          json.dumps(raw, separators=(",", ":"), default=str),
        "decoder":      "vdlm2dec",
    }
    return rec if _has_content(rec) else None


def _normalize_dumpvdl2(raw: dict) -> dict | None:
    """Best-effort dumpvdl2 mapping. UNVERIFIED against live dumpvdl2 output —
    dumpvdl2 cannot drive the Airspy Mini (fixed sample rate), so this path is
    the documented swap target, not the running config. Complete/verify the
    field map against real ``--output decoded:json:...`` before relying on it.
    dumpvdl2 nests ACARS under ``vdl2.avlc.acars`` with the address in
    ``vdl2.avlc.src.addr``.
    """
    vdl2 = raw.get("vdl2") if isinstance(raw.get("vdl2"), dict) else {}
    avlc = vdl2.get("avlc") if isinstance(vdl2.get("avlc"), dict) else {}
    src = avlc.get("src") if isinstance(avlc.get("src"), dict) else {}
    acars = avlc.get("acars") if isinstance(avlc.get("acars"), dict) else {}
    hex_ = clean_short_text(src.get("addr"), _SHORT)
    rec = {
        "ts":           _ts((vdl2.get("t") or {}).get("sec") if isinstance(vdl2.get("t"), dict) else raw.get("timestamp")),
        "icao_hex":     hex_.lower() if hex_ else None,
        "registration": clean_short_text(acars.get("reg"), _SHORT),
        "flight":       clean_short_text(acars.get("flight"), _SHORT),
        "label":        clean_short_text(acars.get("label"), _SHORT),
        "mode":         clean_short_text(acars.get("mode"), _SHORT),
        "block_id":     clean_short_text(acars.get("blk_id"), _SHORT),
        "ack":          clean_short_text(acars.get("ack"), _SHORT),
        "msgno":        clean_short_text(acars.get("msg_num"), _SHORT),
        "freq":         _float_or_none(vdl2.get("freq")),
        "station_id":   clean_short_text(raw.get("station"), _SHORT),
        "toaddr":       clean_short_text((avlc.get("dst") or {}).get("addr") if isinstance(avlc.get("dst"), dict) else None, _SHORT),
        "dsta":         None,
        "lat":          None,
        "lon":          None,
        "alt":          None,
        "epu":          None,
        "app_name":     None,
        "app_ver":      None,
        "body":         clean_short_text(acars.get("msg_text"), config.VDL2_BODY_MAX),
        "raw":          json.dumps(raw, separators=(",", ":"), default=str),
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
