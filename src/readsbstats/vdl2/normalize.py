"""Decoder-agnostic normalization: raw decoder JSON -> one internal record.

Two decoder dialects are supported, selected by ``RSBS_VDL2_DECODER``. dumpvdl2
(nested ``vdl2.avlc.acars.*``) is the recommended, fully mapped decoder: signal and
noise level, sublabel/mfi, XID position/destination, CPDLC and CM logon (ADS-C
reports stay in ``raw`` for query-time parsing).
vdlm2dec (flat JSON) is the limited legacy option — ACARS only: no signal level, no
sublabel/mfi and no CPDLC/CM/ADS-C. Everything downstream of this module is
decoder-agnostic.

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
import math
import time

from .. import config
from ..cleaners import (
    clean_short_text,
    coerce_metric_scalar,
    valid_icao_hex,
    valid_lat,
    valid_lon,
)
from . import atn

_SHORT = 64        # identifier-field cap (reg/flight/label/etc.)


def _ts(value: object) -> int:
    """Coerce an epoch timestamp (vdlm2dec emits a float) to int seconds.
    Falls back to now() when missing/unparseable, and clamps implausible
    *future* values (decoder/clock glitch) to now so a single bad ts can't sort
    to the top of the feed forever. Old timestamps are left as-is (legit backfill)."""
    now = int(time.time())
    parsed: int | None = None
    # NaN/±inf (json.loads accepts NaN/Infinity; "1e999" parses to inf) make int()
    # raise, so treat them as unparseable instead of letting them escape.
    try:
        if isinstance(value, bool):
            parsed = None
        elif isinstance(value, (int, float)):
            parsed = int(value) if math.isfinite(value) else None
        elif isinstance(value, str):
            f = float(value)
            parsed = int(f) if math.isfinite(f) else None
    except (ValueError, OverflowError):
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


def acars_text(body: str | None, sublabel: str | None, mfi: str | None) -> str | None:
    """Rebuild the libacars-stripped DOWNLINK text prefix: ``#<sublabel>B`` plus an
    optional ``/<mfi> `` ahead of ``body``. dumpvdl2 moves that prefix into
    separate ``sublabel``/``mfi`` fields, so prefix-keyed parsers (m1bpos, route,
    kind chips) need it put back. The uplink form (``- #SL``) is intentionally not
    reproduced — this receiver only hears aircraft. Falsy ``body`` or no
    ``sublabel`` → ``body`` unchanged."""
    if not body or not sublabel:
        return body
    return f"#{sublabel}B" + (f"/{mfi} " if mfi else "") + body


def _xid_params(avlc: dict) -> dict:
    """``{name: value}`` from ``avlc.xid.vdl_params``. Every level is type-guarded
    (untrusted upstream JSON): non-dict xid, non-list params, non-dict entries and
    non-str names are skipped."""
    xid = avlc.get("xid")
    params = xid.get("vdl_params") if isinstance(xid, dict) else None
    out: dict = {}
    if isinstance(params, list):
        for p in params:
            if isinstance(p, dict) and isinstance(p.get("name"), str):
                out[p["name"]] = p.get("value")
    return out


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
        # vdlm2dec's JSON carries no signal field — always NULL (the Metrics
        # signal/SNR row self-hides when no rows have a sig_level).
        "sig_level":    None,
        "noise_level":  None,
    }
    return rec if _has_content(rec) else None


def _normalize_dumpvdl2(raw: dict) -> dict | None:
    """Map a dumpvdl2 frame. The ACARS field map + ``vdl2.station`` key below are
    verified against real ``--output decoded:json`` captures (2026-06-14/15 overnight
    run off the Airspy Mini via the iq_tool resample pipe). dumpvdl2 nests ACARS under
    ``vdl2.avlc.acars`` (address in ``vdl2.avlc.src.addr``), reports ``freq`` in **Hz**
    (converted to MHz below), and emits the ``--station-id`` value as ``vdl2.station``.
    ATN/OSI frames (``vdl2.avlc.x25``) carry no ACARS body; CPDLC is lifted into
    ``label``/``body`` via :func:`atn.summarize_cpdlc`; a Context-Management logon
    becomes ``label='CM'``, ``body='LOGON <flight> <dep>-<dest>'`` with ``dsta`` = its
    destination (``flight`` stays NULL: it is an ICAO callsign, not an IATA ACARS id).
    ADS-C and MIAM arrive over-ACARS so the ACARS branch handles them. Other
    ATN transport/routing frames fall through to bare ``icao_hex`` rows by design.

    libacars strips the H1 text prefix into ``acars.sublabel`` (``#M1B`` → ``M1``) and
    ``acars.mfi`` (``/B0 ``), stored as separate columns; ``body`` stays un-prefixed
    (see :func:`acars_text` to rebuild it). XID frames carry the aircraft's coarse
    position (``ac_location``: 0.1° lat/lon, alt in ft) and destination airport
    (``dst_airport``), mapped onto lat/lon/alt/dsta.
    """
    vdl2 = raw.get("vdl2") if isinstance(raw.get("vdl2"), dict) else {}
    avlc = vdl2.get("avlc") if isinstance(vdl2.get("avlc"), dict) else {}
    src = avlc.get("src") if isinstance(avlc.get("src"), dict) else {}
    acars = avlc.get("acars") if isinstance(avlc.get("acars"), dict) else {}
    freq_hz = _float_or_none(vdl2.get("freq"))
    # label/body: ACARS text, else an ATN CPDLC / CM-logon summary for bare ATN frames.
    cm_dest: str | None = None
    if acars:
        label = _label(acars.get("label"))
        body = clean_short_text(acars.get("msg_text"), config.VDL2_BODY_MAX)
    else:
        summary = atn.summarize_cpdlc(avlc)
        if summary is None:
            summary = atn.summarize_cm_logon(avlc)
            if summary is not None:
                logon = atn.cm_logon(avlc)
                cm_dest = logon["dest"] if logon else None
        label = _label(summary[0]) if summary else None
        body = clean_short_text(summary[1], config.VDL2_BODY_MAX) if summary else None
    # XID vdl_params: aircraft position + destination. `gs_location` is the GROUND
    # STATION's position and is deliberately never read.
    xid_p = _xid_params(avlc)
    ac_loc = xid_p.get("ac_location")
    ac_loc = ac_loc if isinstance(ac_loc, dict) else {}
    loc = ac_loc.get("loc") if isinstance(ac_loc.get("loc"), dict) else {}
    rec = {
        "ts":           _ts((vdl2.get("t") or {}).get("sec") if isinstance(vdl2.get("t"), dict) else raw.get("timestamp")),
        # valid_icao_hex enforces 6-hex + lowercases; rejects malformed addrs.
        "icao_hex":     valid_icao_hex(src.get("addr")),
        "registration": _reg(acars.get("reg")),
        "flight":       clean_short_text(acars.get("flight"), _SHORT),
        "label":        label,
        "mode":         clean_short_text(acars.get("mode"), _SHORT),
        "block_id":     clean_short_text(acars.get("blk_id"), _SHORT),
        "ack":          clean_short_text(acars.get("ack"), _SHORT),
        "msgno":        clean_short_text(acars.get("msg_num"), _SHORT),
        "freq":         freq_hz / 1e6 if freq_hz and freq_hz > 0 else None,   # Hz → MHz (0/neg/None → None)
        "station_id":   clean_short_text(vdl2.get("station"), _SHORT),  # dumpvdl2: vdl2.station
        "toaddr":       clean_short_text((avlc.get("dst") or {}).get("addr") if isinstance(avlc.get("dst"), dict) else None, _SHORT),
        # XID dst_airport wins; the CM logon destination is the fallback (the two
        # never co-occur in one real frame, but the precedence is explicit).
        "dsta":         clean_short_text(xid_p.get("dst_airport"), _SHORT) or cm_dest,
        "lat":          valid_lat(loc.get("lat")),
        "lon":          valid_lon(loc.get("lon")),
        "alt":          _int_or_none(ac_loc.get("alt")),
        "epu":          None,
        "app_name":     None,
        "app_ver":      None,
        "body":         body,
        "raw":          _raw_json(raw),
        "decoder":      "dumpvdl2",
        "sublabel":     clean_short_text(acars.get("sublabel"), _SHORT),
        "mfi":          clean_short_text(acars.get("mfi"), _SHORT),
        # Per-frame signal/noise (dBFS), at the vdl2 level alongside freq. Present
        # on every dumpvdl2 frame; feeds the Metrics per-channel signal + SNR charts.
        "sig_level":    _float_or_none(vdl2.get("sig_level")),
        "noise_level":  _float_or_none(vdl2.get("noise_level")),
        # acars.more = a further block of the same message follows. NULL off ACARS
        # (x25.more is an unrelated X.25 flag); feeds vdl2_db.mark_superseded.
        "more":         (1 if acars.get("more") else 0) if acars else None,
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
