"""
ICAO 24-bit aircraft address → country lookup.

Range table sourced from ICAO Annex 10 / dump1090-fa community table
(https://github.com/flightaware/dump1090 / wiseman/aircraft_icao_country).

Sub-ranges (e.g. Bermuda within the UK block) are listed before their
parent block so that the first match wins. Ranges are pre-sorted by size
(ascending) at module load to guarantee that more-specific allocations
always take priority over broader blocks.
"""

from __future__ import annotations
from functools import lru_cache

# (start, end, country_name, iso2)
_RAW: list[tuple[int, int, str, str]] = [
    # Africa
    (0x004000, 0x0043FF, "Zimbabwe",                   "ZW"),
    (0x006000, 0x006FFF, "Mozambique",                 "MZ"),
    (0x008000, 0x00FFFF, "South Africa",               "ZA"),
    (0x010000, 0x017FFF, "Egypt",                      "EG"),
    (0x018000, 0x01FFFF, "Libya",                      "LY"),
    (0x020000, 0x027FFF, "Morocco",                    "MA"),
    (0x028000, 0x02FFFF, "Tunisia",                    "TN"),
    (0x030000, 0x0303FF, "Botswana",                   "BW"),
    (0x032000, 0x032FFF, "Burundi",                    "BI"),
    (0x034000, 0x034FFF, "Cameroon",                   "CM"),
    (0x035000, 0x0353FF, "Comoros",                    "KM"),
    (0x036000, 0x036FFF, "Congo",                      "CG"),
    (0x038000, 0x038FFF, "Cote d'Ivoire",              "CI"),
    (0x03E000, 0x03EFFF, "Gabon",                      "GA"),
    (0x040000, 0x040FFF, "Ethiopia",                   "ET"),
    (0x042000, 0x042FFF, "Equatorial Guinea",          "GQ"),
    (0x044000, 0x044FFF, "Ghana",                      "GH"),
    (0x046000, 0x046FFF, "Guinea",                     "GN"),
    (0x048000, 0x0483FF, "Guinea-Bissau",              "GW"),
    (0x04A000, 0x04A3FF, "Lesotho",                    "LS"),
    (0x04C000, 0x04CFFF, "Kenya",                      "KE"),
    (0x050000, 0x050FFF, "Liberia",                    "LR"),
    (0x054000, 0x054FFF, "Madagascar",                 "MG"),
    (0x058000, 0x058FFF, "Malawi",                     "MW"),
    (0x05C000, 0x05CFFF, "Mali",                       "ML"),
    (0x060000, 0x0603FF, "Mauritius",                  "MU"),
    (0x062000, 0x062FFF, "Niger",                      "NE"),
    (0x064000, 0x064FFF, "Nigeria",                    "NG"),
    (0x068000, 0x068FFF, "Uganda",                     "UG"),
    (0x06A000, 0x06A3FF, "Qatar",                      "QA"),
    (0x06A400, 0x06A7FF, "South Sudan",                "SS"),
    (0x06C000, 0x06CFFF, "Central African Republic",   "CF"),
    (0x06E000, 0x06EFFF, "Rwanda",                     "RW"),
    (0x070000, 0x070FFF, "Senegal",                    "SN"),
    (0x078000, 0x078FFF, "Somalia",                    "SO"),
    (0x07C000, 0x07CFFF, "Sudan",                      "SD"),
    (0x080000, 0x080FFF, "Tanzania",                   "TZ"),
    (0x084000, 0x084FFF, "Chad",                       "TD"),
    (0x088000, 0x088FFF, "Togo",                       "TG"),
    (0x08A000, 0x08AFFF, "Zambia",                     "ZM"),
    (0x08C000, 0x08CFFF, "DR Congo",                   "CD"),
    (0x090000, 0x090FFF, "Angola",                     "AO"),
    (0x094000, 0x0943FF, "Benin",                      "BJ"),
    (0x096000, 0x0963FF, "Cape Verde",                 "CV"),
    (0x09A000, 0x09AFFF, "Gambia",                     "GM"),
    (0x09C000, 0x09CFFF, "Burkina Faso",               "BF"),
    (0x0A0000, 0x0A7FFF, "Algeria",                    "DZ"),
    # Caribbean / Central America
    (0x0A8000, 0x0A8FFF, "Bahamas",                    "BS"),
    (0x0AA000, 0x0AA3FF, "Barbados",                   "BB"),
    (0x0AC000, 0x0ACFFF, "Colombia",                   "CO"),
    (0x0AE000, 0x0AEFFF, "Costa Rica",                 "CR"),
    (0x0B0000, 0x0B0FFF, "Cuba",                       "CU"),
    (0x0B2000, 0x0B2FFF, "El Salvador",                "SV"),
    (0x0B4000, 0x0B4FFF, "Guatemala",                  "GT"),
    (0x0B6000, 0x0B6FFF, "Guyana",                     "GY"),
    (0x0B8000, 0x0B8FFF, "Haiti",                      "HT"),
    (0x0BA000, 0x0BAFFF, "Honduras",                   "HN"),
    (0x0BE000, 0x0BEFFF, "Jamaica",                    "JM"),
    (0x0C0000, 0x0C0FFF, "Nicaragua",                  "NI"),
    (0x0C2000, 0x0C2FFF, "Panama",                     "PA"),
    (0x0C4000, 0x0C4FFF, "Dominican Republic",         "DO"),
    (0x0C6000, 0x0C6FFF, "Trinidad and Tobago",        "TT"),
    (0x0C8000, 0x0C8FFF, "Suriname",                   "SR"),
    (0x0D0000, 0x0D7FFF, "Mexico",                     "MX"),
    (0x0D8000, 0x0DFFFF, "Venezuela",                  "VE"),
    # Russia / Eastern Europe
    (0x100000, 0x1FFFFF, "Russia",                     "RU"),
    # Italy / Spain / France / Germany / UK block
    (0x300000, 0x33FFFF, "Italy",                      "IT"),
    (0x340000, 0x37FFFF, "Spain",                      "ES"),
    (0x380000, 0x3BFFFF, "France",                     "FR"),
    (0x3C0000, 0x3FFFFF, "Germany",                    "DE"),
    # UK large block — sub-ranges listed first so they take priority
    (0x400000, 0x4001BF, "United Kingdom",             "GB"),  # Bermuda sub
    (0x4001C0, 0x4001FF, "United Kingdom",             "GB"),  # Cayman sub
    (0x400300, 0x4003FF, "United Kingdom",             "GB"),  # Turks sub
    (0x424135, 0x4241F2, "United Kingdom",             "GB"),  # Cayman sub
    (0x424200, 0x4246FF, "United Kingdom",             "GB"),  # Bermuda sub
    (0x424700, 0x424899, "United Kingdom",             "GB"),  # Cayman sub
    (0x424B00, 0x424BFF, "United Kingdom",             "GB"),  # IoM sub
    (0x43BE00, 0x43BEFF, "United Kingdom",             "GB"),  # Bermuda sub
    (0x43E700, 0x43EAFD, "United Kingdom",             "GB"),  # IoM sub
    (0x43EAFE, 0x43EEFF, "United Kingdom",             "GB"),  # Guernsey sub
    (0x400000, 0x43FFFF, "United Kingdom",             "GB"),
    # European countries
    (0x440000, 0x447FFF, "Austria",                    "AT"),
    (0x448000, 0x44FFFF, "Belgium",                    "BE"),
    (0x450000, 0x457FFF, "Bulgaria",                   "BG"),
    (0x458000, 0x45FFFF, "Denmark",                    "DK"),
    (0x460000, 0x467FFF, "Finland",                    "FI"),
    (0x468000, 0x46FFFF, "Greece",                     "GR"),
    (0x470000, 0x477FFF, "Hungary",                    "HU"),
    (0x478000, 0x47FFFF, "Norway",                     "NO"),
    (0x480000, 0x487FFF, "Netherlands",                "NL"),
    (0x488000, 0x48FFFF, "Poland",                     "PL"),
    (0x490000, 0x497FFF, "Portugal",                   "PT"),
    (0x498000, 0x49FFFF, "Czechia",                    "CZ"),
    (0x4A0000, 0x4A7FFF, "Romania",                    "RO"),
    (0x4A8000, 0x4AFFFF, "Sweden",                     "SE"),
    (0x4B0000, 0x4B7FFF, "Switzerland",                "CH"),
    (0x4B8000, 0x4BFFFF, "Turkey",                     "TR"),
    (0x4C0000, 0x4C7FFF, "Serbia",                     "RS"),
    (0x4C8000, 0x4C83FF, "Cyprus",                     "CY"),
    (0x4CA000, 0x4CAFFF, "Ireland",                    "IE"),
    (0x4CC000, 0x4CCFFF, "Iceland",                    "IS"),
    (0x4D0000, 0x4D03FF, "Luxembourg",                 "LU"),
    (0x4D2000, 0x4D2FFF, "Malta",                      "MT"),
    (0x4D4000, 0x4D43FF, "Monaco",                     "MC"),
    (0x500000, 0x5003FF, "San Marino",                 "SM"),
    (0x501000, 0x5013FF, "Albania",                    "AL"),
    (0x501C00, 0x501FFF, "Croatia",                    "HR"),
    (0x502C00, 0x502FFF, "Latvia",                     "LV"),
    (0x503C00, 0x503FFF, "Lithuania",                  "LT"),
    (0x504C00, 0x504FFF, "Moldova",                    "MD"),
    (0x505C00, 0x505FFF, "Slovakia",                   "SK"),
    (0x506C00, 0x506FFF, "Slovenia",                   "SI"),
    (0x507C00, 0x507FFF, "Uzbekistan",                 "UZ"),
    (0x508000, 0x50FFFF, "Ukraine",                    "UA"),
    (0x510000, 0x5103FF, "Belarus",                    "BY"),
    (0x511000, 0x5113FF, "Estonia",                    "EE"),
    (0x512000, 0x5123FF, "North Macedonia",            "MK"),
    (0x513000, 0x5133FF, "Bosnia and Herzegovina",     "BA"),
    (0x514000, 0x5143FF, "Georgia",                    "GE"),
    (0x515000, 0x5153FF, "Tajikistan",                 "TJ"),
    (0x516000, 0x5163FF, "Montenegro",                 "ME"),
    # Caucasus / Central Asia
    (0x600000, 0x6003FF, "Armenia",                    "AM"),
    (0x600800, 0x600BFF, "Azerbaijan",                 "AZ"),
    (0x601000, 0x6013FF, "Kyrgyzstan",                 "KG"),
    (0x601800, 0x601BFF, "Turkmenistan",               "TM"),
    (0x683000, 0x6833FF, "Kazakhstan",                 "KZ"),
    # Middle East / Asia
    (0x700000, 0x700FFF, "Afghanistan",                "AF"),
    (0x702000, 0x702FFF, "Bangladesh",                 "BD"),
    (0x704000, 0x704FFF, "Myanmar",                    "MM"),
    (0x706000, 0x706FFF, "Kuwait",                     "KW"),
    (0x710000, 0x717FFF, "Saudi Arabia",               "SA"),
    (0x718000, 0x71FFFF, "South Korea",                "KR"),
    (0x720000, 0x727FFF, "North Korea",                "KP"),
    (0x728000, 0x72FFFF, "Iraq",                       "IQ"),
    (0x730000, 0x737FFF, "Iran",                       "IR"),
    (0x738000, 0x73FFFF, "Israel",                     "IL"),
    (0x740000, 0x747FFF, "Jordan",                     "JO"),
    (0x748000, 0x74FFFF, "Lebanon",                    "LB"),
    (0x750000, 0x757FFF, "Malaysia",                   "MY"),
    (0x758000, 0x75FFFF, "Philippines",                "PH"),
    (0x760000, 0x767FFF, "Pakistan",                   "PK"),
    (0x768000, 0x76FFFF, "Singapore",                  "SG"),
    (0x770000, 0x777FFF, "Sri Lanka",                  "LK"),
    (0x778000, 0x77FFFF, "Syria",                      "SY"),
    (0x789000, 0x789FFF, "Hong Kong",                  "HK"),
    (0x780000, 0x7BFFFF, "China",                      "CN"),
    (0x7C0000, 0x7FFFFF, "Australia",                  "AU"),
    (0x800000, 0x83FFFF, "India",                      "IN"),
    (0x840000, 0x87FFFF, "Japan",                      "JP"),
    (0x880000, 0x887FFF, "Thailand",                   "TH"),
    (0x888000, 0x88FFFF, "Vietnam",                    "VN"),
    (0x890000, 0x890FFF, "Yemen",                      "YE"),
    (0x894000, 0x894FFF, "Bahrain",                    "BH"),
    (0x896000, 0x896FFF, "United Arab Emirates",       "AE"),
    (0x898000, 0x898FFF, "Papua New Guinea",           "PG"),
    (0x899000, 0x8993FF, "Taiwan",                     "TW"),
    (0x8A0000, 0x8A7FFF, "Indonesia",                  "ID"),
    # Americas
    (0xA00000, 0xAFFFFF, "United States",              "US"),
    (0xC00000, 0xC3FFFF, "Canada",                     "CA"),
    (0xC80000, 0xC87FFF, "New Zealand",                "NZ"),
    # South America
    (0xE00000, 0xE3FFFF, "Argentina",                  "AR"),
    (0xE40000, 0xE7FFFF, "Brazil",                     "BR"),
    (0xE80000, 0xE80FFF, "Chile",                      "CL"),
    (0xE84000, 0xE84FFF, "Ecuador",                    "EC"),
    (0xE88000, 0xE88FFF, "Paraguay",                   "PY"),
    (0xE8C000, 0xE8CFFF, "Peru",                       "PE"),
    (0xE90000, 0xE90FFF, "Uruguay",                    "UY"),
    (0xE94000, 0xE94FFF, "Bolivia",                    "BO"),
]

# Sort by range size ascending: smaller (more specific) ranges are checked first,
# so sub-allocations (e.g. Bermuda within the UK block) take priority.
_RANGES: list[tuple[int, int, str, str]] = sorted(
    _RAW, key=lambda r: r[1] - r[0]
)


@lru_cache(maxsize=200_000)
def icao_to_country(icao_hex: str) -> str:
    """Return country name for a 6-digit lowercase ICAO hex address."""
    try:
        addr = int(icao_hex, 16)
    except (ValueError, TypeError):
        return "Unknown"
    for start, end, country, _ in _RANGES:
        if start <= addr <= end:
            return country
    return "Unknown"


def country_sql_case(col: str = "icao_hex") -> str:
    """Return a SQL CASE expression mapping an icao_hex column to a country name.

    Ranges are emitted smallest-first (matching _RANGES sort order) so that
    sub-allocations take priority over their parent blocks.  The expression is
    computed once at import time and can be embedded directly into SQL strings.
    """
    whens = []
    for start, end, country, _ in _RANGES:
        s = format(start, "06x")
        e = format(end, "06x")
        safe = country.replace("'", "''")
        whens.append(f"WHEN {col} >= '{s}' AND {col} <= '{e}' THEN '{safe}'")
    return "CASE " + " ".join(whens) + " ELSE 'Unknown' END"


# Pre-built at import time — embed in SQL queries that need country aggregation.
COUNTRY_SQL_CASE = country_sql_case()


@lru_cache(maxsize=200_000)
def is_anonymous_icao(icao_hex: str | None) -> bool:
    """True if *icao_hex* falls outside every ICAO state-allocated block.

    Such addresses (commonly in the 0xDDxxxx / 0xF0xxxx ranges, plus gaps
    between national blocks) are usually broadcast by military aircraft on
    OPSEC, TIS-B / ADS-R rebroadcasts, or test / non-state operations.  A
    sighting of one is interesting on a civilian ADS-B receiver.

    Returns False on malformed input — invalid hex is not the same as
    intentionally anonymous, and we don't want to noise-flag parser errors.
    """
    if not icao_hex:
        return False
    try:
        addr = int(icao_hex, 16)
    except (ValueError, TypeError):
        return False
    if not (0 <= addr <= 0xFFFFFF):
        return False
    # Audit-13 A13-024: 0x000000 (null / no-information) and 0xFFFFFF
    # (all-call / broadcast) are ICAO-reserved sentinel addresses, not
    # real aircraft. Skip before the state-allocation scan so the badge
    # logic and FLAG_ANONYMOUS retroactive scoring don't tag them.
    if addr in (0x000000, 0xFFFFFF):
        return False
    for start, end, _country, _iso in _RANGES:
        if start <= addr <= end:
            return False
    return True


def anonymous_flag_sql(col: str = "icao_hex", flag_value: int = 16) -> str:
    """Return a SQL CASE expression that evaluates to *flag_value* when *col*
    is outside every state-allocated block, else 0.  Mirrors
    :func:`is_anonymous_icao` for SQLite — embed inside an OR-merged flags
    expression to surface the anon bit without a stored column.

    *flag_value* defaults to 16 to match ``config.FLAG_ANONYMOUS``.  Callers
    that want a different bit value pass it explicitly so the constant stays
    in one place (``config.py``).
    """
    # Audit-13 A13-024: ICAO-reserved sentinel addresses (null + broadcast)
    # are not real aircraft — mirror the Python guard so SQL queries don't
    # tag them either. Listed first so subsequent state-block WHENs aren't
    # consulted for these two values.
    whens = [
        f"WHEN {col} IN ('000000', 'ffffff') THEN 0",
    ]
    for start, end, _country, _iso in _RANGES:
        s = format(start, "06x")
        e = format(end, "06x")
        whens.append(f"WHEN {col} >= '{s}' AND {col} <= '{e}' THEN 0")
    return f"CASE {' '.join(whens)} ELSE {int(flag_value)} END"
