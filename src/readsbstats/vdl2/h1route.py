"""Reported dep/dest from four ACARS formats that carry a city pair (ADR-0019, Batch 7).

Each parser yields ``{"dep_icao", "dest_icao", "source"}``. They rank BELOW every existing
route source (CM logon, label 26/20/49) in ``/api/vdl2/oooi``: ACARS is treated as
authoritative, but these formats were validated only against the flights table's
callsign-enriched ``origin``/``dest`` (snapshot 2026-11-23), and the disagreements were
dominated by enrichment errors (charter callsigns, stale ICAO UTTT vs UZTT, BAW844), so the
agreement rate is a lower bound on parser accuracy. Order among them follows that rate:

=========  ====================================================  ===========  ======
source     format                                                agreement    n
=========  ====================================================  ===========  ======
``r64``    ``H1``/sublabel ``T8``, body ``R64…``, CSV fields 8/9  100%         200
``abs``    ``H1``/``DF``, ``ABS``/``EB0``/``SB0`` header line     87% (CSV),   --
                                                                  86% (fixed)
``b44``    ``H1``/``DF``, body ``B44…``, 2nd line ``…EPWALPPTTO…`` 85%         --
``fst01``  label ``15``, ``FST01<DEP><ARR>…``                     61%         --
=========  ====================================================  ===========  ======

Dispatch is on ``(label, sublabel, body prefix)``, never body shape alone, so the same text
under another label/sublabel is ignored. dumpvdl2 only: its libacars moves the H1 prefix
into ``sublabel``, which these parsers key on; vdlm2dec rows have no sublabel and yield
nothing. Query-time and read-only.
"""
from __future__ import annotations

import re

_ICAO = re.compile(r"[A-Z]{4}\Z")
_ABS_PREFIXES = ("ABS", "EB0", "SB0")
# Fixed dialect header: ``ABS011DA_S       EPWALIRF4`` / ``EB0031AA_ SP-LRG EPWAKMIA1``.
_ABS_FIXED = re.compile(r"(?<![A-Z])([A-Z]{4})([A-Z]{4})\d*\s*\Z")
# Second line of B44: ``SP-LVH  4824HT EPWALPPTTO140926122650`` (TO/TA + DDMMYY + HHMMSS).
_B44 = re.compile(r"\b([A-Z]{4})([A-Z]{4})T[OA]\d{12}")
_FST01 = re.compile(r"FST01([A-Z]{4})([A-Z]{4})")


def _pair(dep: object, dest: object, source: str) -> dict | None:
    if (not isinstance(dep, str) or not isinstance(dest, str)
            or not _ICAO.match(dep) or not _ICAO.match(dest) or dep == dest):
        return None
    return {"dep_icao": dep, "dest_icao": dest, "source": source}


def _lines(body: str) -> list[str]:
    return re.split(r"\r?\n", body, maxsplit=2)


def _parse_abs(body: str) -> dict | None:
    """Header line only. CSV (LOT 737 MAX): comma fields 4/5. Fixed: trailing
    ``<DEP4><ARR4><digits>``. Validation vs flights: CSV 87%, fixed 86%."""
    header = _lines(body)[0]
    if "," in header:
        fields = header.split(",")
        if len(fields) < 6:
            return None
        return _pair(fields[4].strip(), fields[5].strip(), "abs")
    m = _ABS_FIXED.search(header)
    return _pair(m[1], m[2], "abs") if m else None


def _parse_b44(body: str) -> dict | None:
    """Second line ``… EPWALPPTTO140926122650``. Validation vs flights: 85%."""
    lines = _lines(body)
    if len(lines) < 2:
        return None
    m = _B44.search(lines[1])
    return _pair(m[1], m[2], "b44") if m else None


def _parse_r64(body: str) -> dict | None:
    """``R641V0106,…,EPWA,LFPG,17:43:03,…``: comma fields 8/9. Validation vs flights:
    100% (n=200)."""
    fields = body.split(",")
    if len(fields) < 10:
        return None
    return _pair(fields[8].strip(), fields[9].strip(), "r64")


def _parse_fst01(body: str) -> dict | None:
    """``FST01EGLLEPWAN52232E…`` / ``FST01EPWAEGLL 00 …`` (position optional).
    Validation vs flights: 61%."""
    m = _FST01.match(body)
    return _pair(m[1], m[2], "fst01") if m else None


def parse_route(label: object, sublabel: object, body: object) -> dict | None:
    """Route of one row as ``{"dep_icao","dest_icao","source"}`` or ``None``.
    ``source`` is one of ``abs``, ``b44``, ``r64``, ``fst01``."""
    if not isinstance(body, str) or not body or not isinstance(label, str):
        return None
    if label == "H1" and isinstance(sublabel, str):
        if sublabel == "DF":
            if body.startswith(_ABS_PREFIXES):
                return _parse_abs(body)
            if body.startswith("B44"):
                return _parse_b44(body)
        elif sublabel == "T8" and body.startswith("R64"):
            return _parse_r64(body)
    elif label == "15":
        return _parse_fst01(body)
    return None
