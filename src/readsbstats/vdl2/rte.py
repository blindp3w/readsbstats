"""Parse RTE (Teledyne AID route) ACARS bodies into a filed route.

These bodies carry a flight's filed departure/arrival + company route, e.g.
``RTE 1 05JUN26 1306 SP-LVS LOT377 EPWA/EDDF BCG59-U000-08E7 BCG38-0MFC-0017 L …``
(also seen `#T1B`-prefixed). The client airframes decoder leaves them undecoded, so
this is the only path to the route. Output matches :func:`m1bpos.parse_route`'s shape
(``dep``/``arr`` + optional ``company_route``) so it feeds the same ``filed_route``
field and ``FiledRoute`` UI.

Read-only, parsed at query time (same pattern as m1bpos.py / oooi.py / positions.py).
Scope: `#T1B`-prefixed and bare ``RTE `` forms — the ones the feed carries.
"""
from __future__ import annotations

import re

from ..cleaners import clean_short_text

# DEP/ARR is the first ICAO/ICAO pair on the header line; route codes (e.g.
# 'BCG59-U000-08E7') contain no '/', so the first slash-pair is always dep/arr.
_DEPARR = re.compile(r"\b([A-Z]{4})/([A-Z]{4})\b\s*(.*)$")
_ROUTE_CAP = 200  # company-route strings run ~30-70 chars


def parse_route(body: object) -> dict | None:
    """Extract ``{"dep", "arr", "company_route"?}`` from an RTE body, or ``None``
    when it carries no parseable route."""
    if not isinstance(body, str):
        return None
    s = body[4:] if body.startswith("#T1B") else body
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
