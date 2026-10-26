"""Parse RTE (Teledyne AID route) ACARS bodies into a filed route.

These bodies carry a flight's filed departure/arrival + company route, e.g.
``RTE 1 05JUN26 1306 SP-LVS LOT377 EPWA/EDDF BCG59-U000-08E7 BCG38-0MFC-0017 L …``
(also seen behind an H1 prefix such as `#T1B` or `#CFB`). The client airframes decoder leaves them undecoded, so
this is the only path to the route. Output matches :func:`m1bpos.parse_route`'s shape
(``dep``/``arr`` + optional ``company_route``) so it feeds the same ``filed_route``
field and ``FiledRoute`` UI.

Read-only, parsed at query time (same pattern as m1bpos.py / oooi.py / positions.py).
Scope: bare ``RTE `` bodies and ``RTE`` behind any H1 ``#<SL>B`` prefix with an
optional ``/<MFI> `` (the form :func:`normalize.acars_text` rebuilds from dumpvdl2's
``sublabel``/``mfi`` columns — the sublabel varies: ``T1``, ``CF``, …).
"""
from __future__ import annotations

import re

from ..cleaners import clean_short_text

# DEP/ARR is the first ICAO/ICAO pair on the header line; route codes (e.g.
# 'BCG59-U000-08E7') contain no '/', so the first slash-pair is always dep/arr.
_DEPARR = re.compile(r"\b([A-Z]{4})/([A-Z]{4})\b\s*(.*)$")
_ROUTE_CAP = 200  # company-route strings run ~30-70 chars
# H1 text prefix: '#<2 alnum sublabel>B' + optional '/<MFI> ' (cf. normalize.acars_text).
_H1_PREFIX = re.compile(r"#[A-Z0-9]{2}B(?:/[A-Za-z0-9]+ )?")


def _strip_prefix(text: str) -> str:
    m = _H1_PREFIX.match(text)
    return text[m.end():] if m else text


def is_rte(text: object) -> bool:
    """True when ``text`` is an RTE body, bare or behind an H1 ``#<SL>B[/<MFI> ]``
    prefix. Cheap dispatch check for callers before :func:`parse_route`."""
    return isinstance(text, str) and _strip_prefix(text).startswith("RTE ")


def parse_route(body: object) -> dict | None:
    """Extract ``{"dep", "arr", "company_route"?}`` from an RTE body, or ``None``
    when it carries no parseable route."""
    if not isinstance(body, str):
        return None
    s = _strip_prefix(body)
    if not s.startswith("RTE "):
        return None
    first = s.split("\n", 1)[0]
    m = _DEPARR.search(first)
    if m is None:
        return None
    dep, arr, rest = m.group(1), m.group(2), m.group(3).strip()
    out: dict[str, str] = {"dep": dep, "arr": arr}
    # Trim the trailing ' L <time> <date>' bookkeeping that follows the route codes.
    company = clean_short_text(re.split(r"\s+L\s+\d", rest)[0].strip(), _ROUTE_CAP)
    if company:
        out["company_route"] = company
    return out
