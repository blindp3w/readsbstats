"""OOOI (Out/Off/On/In) block-time parsing from ACARS message bodies.

Three formats, in order of what a real air-side VDL2 feed actually carries
(validated against a 6.4-day live dump, 13.6k msgs, 2026-06):

1. **Q-series compact reports** (``parse_qseries``) — labels QP=OUT, QQ=OFF,
   QR=ON, QS=IN; body ``<dep ICAO×4><arr ICAO×4><HHMM>[…]``, e.g.
   ``LIRAEPMO2106``. Ryanair/Wizz use these heavily (~122 msgs/6 d). QQ (OFF)
   bodies append a second HHMM echoing the earlier OUT time
   (``QP EPMOGCTS1059`` → ``QQ EPMOGCTS11121059``), so one QQ yields both
   ``t_off`` and ``t_out``.
2. **Label 49 movement reports** (``parse_label49``) — airline-defined (NOT
   ARINC-standard); the observed Etihad/LOT form is
   ``01DCAP    ETD159/090545OMAAEPWA`` = flight/DDHHMM + dep+arr pair. Used as
   a route source only — no OOOI times are derived from it.
3. **Slash-delimited TEI** (``parse_oooi``) — e.g.
   ``DEP / FI JA401/AN CC-AWE/DA SPJC/DS SCEL/OT 0030`` with ``AN``=registration,
   ``DA``=departure, ``DS``/``AD``=destination, ``FI``=flight, ``OT``/``OFF``/
   ``ON``/``IN``=times. This is the ARINC-620 *ground-side* Standard Message
   Text; an air-side receiver near LOT traffic sees ~0 of these (downlinks are
   proprietary Teledyne ACMS ``#DFB``/``#CFB`` blocks). Kept as the
   highest-precedence parser for carriers that do emit it.

All parsers are deliberately conservative and fail SOFT to ``None`` so a noisy
free-text feed never produces bogus OOOI cards. Airport idents are matched as
strict ``[A-Z]{4}`` on the raw body — NOT via ``cleaners.valid_icao_code``,
which accepts alphanumerics and case-normalizes (too permissive here: lowercase
junk must reject, digit-bearing "idents" must reject).
"""
from __future__ import annotations

import re

from ..cleaners import clean_short_text

# TEI keys are 2 OR 3 letters (OFF is 3). The key is the MAXIMAL run of 2-3
# uppercase letters immediately before the value-separating whitespace: for
# `ON 0210` the run is just `ON` (the 3rd char is a space, not [A-Z], so {2,3}
# matches 2 directly — no backtracking involved); for `OFF 0030` it's the full
# 3-letter `OFF`. A 3-letter run that ISN'T an OOOI key (e.g. a stray `ABC`) is
# captured whole and then DROPPED by the `key in _OOOI_KEYS` membership check
# below — it is never re-interpreted as a 2-letter key.
_FIELD = re.compile(r"^([A-Z]{2,3})\s+(\S.*)$")
_OOOI_KEYS = frozenset({"AN", "FI", "DA", "DS", "AD", "OT", "OFF", "ON", "IN"})
_ID_CAP = 16   # registration / flight / airport idents are short

# Q-series label → OOOI phase. Public: api/vdl2.py dispatches on membership.
Q_PHASES = {"QP": "out", "QQ": "off", "QR": "on", "QS": "in"}

_HHMM = r"(?:[01]\d|2[0-3])[0-5]\d"
# dep+arr ICAO, HHMM, optional second HHMM (QQ carries an OUT-time echo). The
# tail must be empty or start with whitespace / digit / slash (observed: " 192",
# "  96", "/FB   71", a newline + position line) — a letter right after the
# time(s) rejects the whole body.
_QSERIES_RE = re.compile(rf"^([A-Z]{{4}})([A-Z]{{4}})({_HHMM})({_HHMM})?(?=$|[\s\d/])")
# Label 49 is airline-defined (not ARINC-standard); this matches the observed
# Etihad/LOT movement form only — regex strictness is the safety net against
# other carriers' incompatible label-49 bodies. Flight id allows up to 2
# trailing letters (DLH4AB-style).
_LABEL49_RE = re.compile(r"\b([A-Z]{3}\d{1,4}[A-Z]{0,2})/\d{6}([A-Z]{4})([A-Z]{4})(?![A-Z0-9])")


def parse_oooi(body: object) -> dict | None:
    """Parse an OOOI ``DEP``/``ARR`` body into a structured record, or return
    ``None`` if *body* isn't a recognisable OOOI report.

    Returns a dict with: ``type`` ('DEP'|'ARR'), ``registration``, ``flight``,
    ``dep_icao``, ``dest_icao``, and the four times ``t_out``/``t_off``/``t_on``/
    ``t_in`` (each ``None`` when that TEI is absent). Requires the exact lead
    token plus at least one recognised TEI field — otherwise ``None``."""
    if not isinstance(body, str) or not body:
        return None
    segments = [s.strip() for s in body.split("/")]
    if not segments:  # pragma: no cover — str.split always yields ≥1 element
        return None
    lead = segments[0].upper()
    if lead not in ("DEP", "ARR"):
        return None

    fields: dict[str, str] = {}
    for seg in segments[1:]:
        m = _FIELD.match(seg)
        if m:
            key = m.group(1).upper()
            if key in _OOOI_KEYS:
                fields[key] = m.group(2).strip()
    if not fields:
        return None

    rec = {
        "type": lead,
        "registration": clean_short_text(fields.get("AN"), _ID_CAP),
        "flight": clean_short_text(fields.get("FI"), _ID_CAP),
        "dep_icao": clean_short_text(fields.get("DA"), _ID_CAP),
        # DS (destination station) preferred; AD (arrival aerodrome) is the
        # ARR-side equivalent. Whichever is present wins.
        "dest_icao": clean_short_text(fields.get("DS") or fields.get("AD"), _ID_CAP),
        "t_out": clean_short_text(fields.get("OT"), _ID_CAP),
        "t_off": clean_short_text(fields.get("OFF"), _ID_CAP),
        "t_on": clean_short_text(fields.get("ON"), _ID_CAP),
        "t_in": clean_short_text(fields.get("IN"), _ID_CAP),
    }
    return rec


def parse_qseries(label: object, body: object) -> dict | None:
    """Parse a Q-series compact OOOI body (label QP/QQ/QR/QS) into a phase
    partial, or return ``None`` when *label* isn't a Q-series OOOI label or
    *body* doesn't conform.

    Returns ``{"phase": "out"|"off"|"on"|"in", "dep_icao", "dest_icao",
    "t": HHMM, "t2": HHMM|None}`` — ``t2`` is the OUT-time echo that QQ (OFF)
    reports append; it is ``None`` for every other label even when a second
    time group is present (no evidence it means anything there)."""
    phase = Q_PHASES.get(label) if isinstance(label, str) else None
    if phase is None or not isinstance(body, str) or not body:
        return None
    m = _QSERIES_RE.match(body.strip())
    if m is None:
        return None
    return {
        "phase": phase,
        "dep_icao": m.group(1),
        "dest_icao": m.group(2),
        "t": m.group(3),
        "t2": m.group(4) if label == "QQ" else None,
    }


def parse_label49(body: object) -> dict | None:
    """Parse an airline-defined label-49 movement report into a route record
    ``{"flight", "dep_icao", "dest_icao"}``, or ``None``.

    Route source only: the DDHHMM group anchors the format but is deliberately
    unused — its event semantics (ETD? report time?) are unconfirmed, so no
    OOOI times are invented from it."""
    if not isinstance(body, str) or not body:
        return None
    m = _LABEL49_RE.search(body)
    if m is None:
        return None
    return {
        "flight": m.group(1),
        "dep_icao": m.group(2),
        "dest_icao": m.group(3),
    }
