"""Summaries for dumpvdl2 ATN/OSI frames — the classes vdlm2dec can't decode.

vdlm2dec sees only AVLC **ACARS**; dumpvdl2 (+ libacars) also decodes the ATN/OSI
stack (X.25 → CLNP → COTP → CPDLC). Those frames carry no ``acars.msg_text``, so
``normalize._normalize_dumpvdl2`` would otherwise store them as content-free
``icao_hex``-only rows. This module lifts the human-readable intent out of a CPDLC
frame so the row gets a real ``label``/``body`` and shows up like any other message.
An ATN Context-Management **logon** (``cm_logon``) is lifted the same way: it carries
the aircraft's own FMS flight id plus departure and destination airports, which the
OOOI summary serves as a reported route.

Unlike ``oooi.py`` / ``m1bpos.py`` / ``positions.py`` (which parse the stored ``body``
text at query time), the ATN payload is structured JSON living only in ``raw`` — so
this is called at **ingest** from the normalizer, against the decoder dict.

CPDLC and CM logon only. ADS-C arrives over ACARS (handled by ``adsc.py``); MIAM too,
via the ACARS branch.
"""
from __future__ import annotations

import re

from ..cleaners import clean_short_text

_CPDLC_LABEL = "CPDLC"
_CM_LABEL = "CM"

# Strict shapes for untrusted logon fields (all 5,560 real frames matched): airports
# are 4 uppercase letters; the flight id is an ICAO callsign (RYR8414), 2-8 chars.
_AIRPORT_RE = re.compile(r"[A-Z]{4}")
_CM_FLIGHT_RE = re.compile(r"[A-Z0-9]{2,8}")
_CM_FLIGHT_CAP = 16


def summarize_cpdlc(avlc: object) -> tuple[str, str] | None:
    """ATN CPDLC summary for a dumpvdl2 AVLC frame, or ``None`` when absent/undecodable.

    Returns ``(label, body)`` where ``label='CPDLC'`` and ``body`` is the message
    elements' intents joined (e.g. ``'WILCO'``, ``'CURRENT DATA AUTHORITY; WILCO'``).
    Path: ``avlc.x25.clnp.cotp.cpdlc.{atc_downlink_message|atc_uplink_message}``
    ``.msg_data.msg_elements[].msg_element.choice_label``. Every level is guarded, so
    any structural miss (incl. a ``cpdlc`` key with no decodable ``msg_elements``, or
    an element lacking ``choice_label``) yields ``None`` and the row stays bare.
    """
    if not isinstance(avlc, dict):
        return None
    x25 = avlc.get("x25")
    clnp = x25.get("clnp") if isinstance(x25, dict) else None
    cotp = clnp.get("cotp") if isinstance(clnp, dict) else None
    cpdlc = cotp.get("cpdlc") if isinstance(cotp, dict) else None
    if not isinstance(cpdlc, dict):
        return None
    msg = cpdlc.get("atc_downlink_message")
    if not isinstance(msg, dict):
        msg = cpdlc.get("atc_uplink_message")
    if not isinstance(msg, dict):
        return None
    msg_data = msg.get("msg_data")
    elements = msg_data.get("msg_elements") if isinstance(msg_data, dict) else None
    if not isinstance(elements, list):
        return None
    intents: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        me = el.get("msg_element")
        label = me.get("choice_label") if isinstance(me, dict) else None
        if isinstance(label, str) and label.strip():
            intents.append(label.strip())
    if not intents:
        return None
    return _CPDLC_LABEL, "; ".join(intents)


def cm_logon(avlc: object) -> dict | None:
    """Fields of an ATN Context-Management logon request, or ``None``.

    Returns ``{"flight", "dep", "dest"}`` (each ``str | None``). Path:
    ``avlc.x25.clnp.cotp.x225_spdu.x227_apdu.context_mgmt.cm_aircraft_message`` with
    ``choice == 'cmLogonRequest'`` and the fields under ``data.atn_context_mgmt_logon_request``
    (``context_mgmt`` is a sibling of ``acse_apdu`` inside ``x227_apdu``). Every level is
    guarded; airports must fully match ``[A-Z]{4}`` and the flight id ``[A-Z0-9]{2,8}``,
    else that field is ``None``. Any other choice or structural miss yields ``None``."""
    if not isinstance(avlc, dict):
        return None
    node: object = avlc
    for key in ("x25", "clnp", "cotp", "x225_spdu", "x227_apdu", "context_mgmt",
                "cm_aircraft_message"):
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict) or node.get("choice") != "cmLogonRequest":
        return None
    data = node.get("data")
    logon = data.get("atn_context_mgmt_logon_request") if isinstance(data, dict) else None
    if not isinstance(logon, dict):
        return None

    def airport(value: object) -> str | None:
        return value if isinstance(value, str) and _AIRPORT_RE.fullmatch(value) else None

    flight = clean_short_text(logon.get("flight_id"), _CM_FLIGHT_CAP)
    if flight is not None and not _CM_FLIGHT_RE.fullmatch(flight):
        flight = None
    return {
        "flight": flight,
        "dep": airport(logon.get("departure_airport")),
        "dest": airport(logon.get("destination_airport")),
    }


def summarize_cm_logon(avlc: object) -> tuple[str, str] | None:
    """ATN CM logon summary ``(label, body)`` for a dumpvdl2 AVLC frame, or ``None``.

    ``label='CM'``; ``body`` is ``'LOGON <flight> <dep>-<dest>'`` when all three are
    known, ``'LOGON <flight>'`` when the route is incomplete, bare ``'LOGON'`` with no
    flight. ``oooi.parse_cm_logon`` reads the full form back at query time."""
    logon = cm_logon(avlc)
    if logon is None:
        return None
    flight, dep, dest = logon["flight"], logon["dep"], logon["dest"]
    if flight is None:
        return _CM_LABEL, "LOGON"
    if dep is None or dest is None:
        return _CM_LABEL, f"LOGON {flight}"
    return _CM_LABEL, f"LOGON {flight} {dep}-{dest}"
