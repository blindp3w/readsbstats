"""Summaries for dumpvdl2 ATN/OSI frames — the classes vdlm2dec can't decode.

vdlm2dec sees only AVLC **ACARS**; dumpvdl2 (+ libacars) also decodes the ATN/OSI
stack (X.25 → CLNP → COTP → CPDLC). Those frames carry no ``acars.msg_text``, so
``normalize._normalize_dumpvdl2`` would otherwise store them as content-free
``icao_hex``-only rows. This module lifts the human-readable intent out of a CPDLC
frame so the row gets a real ``label``/``body`` and shows up like any other message.

Unlike ``oooi.py`` / ``m1bpos.py`` / ``positions.py`` (which parse the stored ``body``
text at query time), the ATN payload is structured JSON living only in ``raw`` — so
this is called at **ingest** from the normalizer, against the decoder dict.

Currently CPDLC only. ADS-C is deferred (no real frame captured yet); MIAM arrives
over-ACARS and is already handled by the ACARS branch.
"""
from __future__ import annotations

_CPDLC_LABEL = "CPDLC"


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
