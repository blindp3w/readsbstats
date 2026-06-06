"""OOOI (Out/Off/On/In) block-time parsing from ACARS message bodies.

OOOI reports are NOT identified by the ACARS ``label`` (dominantly ``H1`` on a
European AOC feed). They appear as slash-delimited TEI key-values inside the
free-text ``body``, e.g.::

    DEP / FI JA401/AN CC-AWE/DA SPJC/DS SCEL/OT 0030

Verified TEI map (vdl2-research.md §3, OAG ACARS OOOI doc):
``AN``=registration, ``DA``=departure aerodrome, ``DS``=destination station,
``AD``=arrival aerodrome, ``FI``=carrier+flight number, and ``OT``/``OFF``/``ON``/
``IN`` = the four OOOI times.

EXPERIMENTAL — and validated against a real vdlm2dec LOT feed (413 msgs/4.6 h):
**0 matches.** The slash-TEI form above is the ARINC-620 *ground-side* Standard
Message Text (what an aggregator/airline host sees). A VDL2 receiver captures the
raw *air-side* downlink, which for this carrier is proprietary Teledyne ACMS
(``#DFB``/``#CFB``/``#T1B`` …) — OOOI block times are embedded there, not as
SMT/TEI. So this parser is correct but commonly empty on air-side feeds; the only
OOOI-class signal that reliably fires is the ``dsta`` destination from XID frames
(surfaced as the card's fallback). The parser is deliberately conservative —
recognises only an exact ``DEP``/``ARR`` lead token and fails SOFT to ``None`` —
so a noisy free-text feed never produces bogus OOOI cards.
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
    if not segments:
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
