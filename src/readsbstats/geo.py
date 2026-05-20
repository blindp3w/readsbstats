"""
readsbstats — shared geometry helpers.
"""

import math

EARTH_RADIUS_NM = 3440.065


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles between two lat/lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_NM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees (0 = N, clockwise) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def haversine_sql(lat_col: str, lon_col: str,
                  ref_lat_param: str = ":rlat", ref_lon_param: str = ":rlon") -> str:
    """Return a SQL expression for great-circle distance in nautical miles.

    Audit-13 A13-076: single source of truth for the inline haversine
    SQL formula used by `analytics.py` and `web.py`. Both engines
    (SQLite + DuckDB) parse this identically.

    `lat_col` / `lon_col` are SQL column refs (e.g. ``"p.lat"``).
    `ref_*_param` are bind-parameter placeholders or numeric literals.
    """
    return (
        f"({EARTH_RADIUS_NM} * 2 * asin(sqrt("
        f"  sin(radians(({lat_col} - {ref_lat_param}) / 2)) * sin(radians(({lat_col} - {ref_lat_param}) / 2))"
        f"+ cos(radians({ref_lat_param})) * cos(radians({lat_col}))"
        f"* sin(radians(({lon_col} - {ref_lon_param}) / 2)) * sin(radians(({lon_col} - {ref_lon_param}) / 2))"
        f")))"
    )


def bearing_sql(lat_col: str, lon_col: str,
                ref_lat_param: str = ":rlat", ref_lon_param: str = ":rlon") -> str:
    """Return a SQL expression for the initial bearing in degrees (0=N).

    Audit-13 A13-076: shared with `haversine_sql`.
    """
    return (
        f"((degrees(atan2("
        f"  sin(radians({lon_col} - {ref_lon_param})) * cos(radians({lat_col})),"
        f"  cos(radians({ref_lat_param})) * sin(radians({lat_col}))"
        f"  - sin(radians({ref_lat_param})) * cos(radians({lat_col}))"
        f"    * cos(radians({lon_col} - {ref_lon_param}))"
        f")) + 360) % 360)"
    )


def destination_point(
    lat: float, lon: float, bearing_deg: float, dist_nm: float
) -> tuple[float, float]:
    """Return (lat, lon) of the point `dist_nm` nm from (lat, lon) on bearing `bearing_deg`."""
    d = dist_nm / EARTH_RADIUS_NM
    b = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b)
    )
    lon2 = lon1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180
