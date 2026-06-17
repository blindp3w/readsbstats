"""Pydantic response contracts for the hot ``/api/*`` endpoints (FE-2).

These models exist to publish a typed OpenAPI schema (the contract the
frontend builds its interfaces against) **without changing the JSON the
handlers already emit**. The no-behaviour-change guarantee rests on two
settings used together on every endpoint:

* every model inherits :class:`ApiModel` (``extra="allow"``) so a column
  the model does not explicitly declare is *preserved*, never dropped —
  adding a SELECT column can't silently disappear from the response;
* every endpoint sets ``response_model_exclude_unset=True`` so a field the
  model declares but the handler's dict omits is *not* injected as ``null``.

Together these mean the emitted key set is exactly the handler dict's key
set (verified by the key-parity tests in ``tests/test_web.py``). Declared
fields still get Pydantic type coercion and show up in ``/openapi.json``.

Highly dynamic, SQL-row-shaped collections (``top_airlines``, ``heatmap``,
``furthest_aircraft`` …) are typed as ``list[dict]`` / ``dict`` passthroughs
on purpose: their inner shape varies with enrichment joins and the frontend
already owns hand-written interfaces for them, so recursing a strict model
there would add drop-risk and Pi-4 serialization cost for no real contract
gain.

Phase 6 (``api/*`` router split) imports these from here.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """Base for every response contract — preserves undeclared keys.

    ``extra="allow"`` keeps DB-derived columns the model doesn't name; pair
    with ``response_model_exclude_unset=True`` at the endpoint for exact
    key-parity with the handler's dict.
    """

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Positions (split endpoints + opt-in embed)
# ---------------------------------------------------------------------------

class PositionChart(ApiModel):
    """A single downsampled position row (``/positions/chart``).

    Mirrors the chart SELECT, which omits ``rssi`` (the chart/map don't use
    it). ``PositionFull`` adds it back for the inspection table.
    """

    ts: int
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_baro: Optional[int] = None
    alt_geom: Optional[int] = None
    gs: Optional[float] = None
    track: Optional[float] = None
    baro_rate: Optional[int] = None
    source_type: Optional[str] = None


class PositionFull(PositionChart):
    """A raw position row for the paginated inspection table (``/positions``)."""

    rssi: Optional[float] = None


class FlightPositionsResponse(ApiModel):
    total: int
    limit: int
    offset: int
    positions: list[PositionFull]


class FlightPositionsChartResponse(ApiModel):
    total: int
    target: int
    positions: list[PositionChart]


# ---------------------------------------------------------------------------
# Flight detail
# ---------------------------------------------------------------------------

class FlightMeta(ApiModel):
    """Enriched flight row (``_FLIGHT_COLS`` + ``airline_name``).

    Used for both the detail ``flight`` object and each ``other_flights``
    entry. ``other_flights`` rows carry no ``airline_name`` key; with
    ``exclude_unset`` it stays absent there rather than appearing as null.
    """

    id: int
    icao_hex: str
    callsign: Optional[str] = None
    registration: Optional[str] = None
    aircraft_type: Optional[str] = None
    type_desc: Optional[str] = None
    flags: Optional[int] = None
    squawk: Optional[str] = None
    category: Optional[str] = None
    primary_source: Optional[str] = None
    first_seen: Optional[int] = None
    last_seen: Optional[int] = None
    duration_sec: Optional[int] = None
    max_alt_baro: Optional[int] = None
    max_gs: Optional[float] = None
    max_distance_nm: Optional[float] = None
    total_positions: Optional[int] = None
    adsb_positions: Optional[int] = None
    mlat_positions: Optional[int] = None
    lat_min: Optional[float] = None
    lat_max: Optional[float] = None
    lon_min: Optional[float] = None
    lon_max: Optional[float] = None
    origin_icao: Optional[str] = None
    dest_icao: Optional[str] = None
    origin_name: Optional[str] = None
    origin_country: Optional[str] = None
    dest_name: Optional[str] = None
    dest_country: Optional[str] = None
    airline_name: Optional[str] = None


class FlightDetailResponse(ApiModel):
    flight: FlightMeta
    positions: list[PositionFull]
    other_flights: list[FlightMeta]
    receiver_lat: Optional[float] = None
    receiver_lon: Optional[float] = None


# ---------------------------------------------------------------------------
# Photo (specific-ICAO or type-level fallback — polymorphic columns)
# ---------------------------------------------------------------------------

class PhotoResponse(ApiModel):
    """Aircraft photo. A specific-ICAO hit and a ``type_photos`` fallback
    carry slightly different column sets; ``extra="allow"`` + exclude_unset
    keep whichever keys the source actually returned."""

    icao_hex: Optional[str] = None
    thumbnail_url: Optional[str] = None
    large_url: Optional[str] = None
    link_url: Optional[str] = None
    photographer: Optional[str] = None
    fetched_at: Optional[int] = None
    is_type_photo: Optional[bool] = None
    type_code: Optional[str] = None
    type_desc: Optional[str] = None


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

class WatchlistItem(ApiModel):
    id: int
    match_type: str
    value: str
    label: Optional[str] = None
    created_at: Optional[int] = None
    # 0/1 ints (the JSON shape the SQL CASE emits), not bools.
    airborne: Optional[int] = None


class WatchlistListResponse(ApiModel):
    entries: list[WatchlistItem]


# ---------------------------------------------------------------------------
# Map snapshot
# ---------------------------------------------------------------------------

class MapSnapshotAircraft(ApiModel):
    flight_id: int
    ts: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_baro: Optional[int] = None
    gs: Optional[float] = None
    track: Optional[float] = None
    source_type: Optional[str] = None
    icao_hex: Optional[str] = None
    callsign: Optional[str] = None
    registration: Optional[str] = None
    aircraft_type: Optional[str] = None
    category: Optional[str] = None
    primary_source: Optional[str] = None
    flags: Optional[int] = None
    origin_icao: Optional[str] = None
    dest_icao: Optional[str] = None
    seconds_ago: Optional[int] = None
    # Each trail point is ``[lat, lon, ts]``; kept as a raw list so the int
    # ``ts`` isn't coerced to float.
    trail: list[Any] = Field(default_factory=list)


class MapSnapshotResponse(ApiModel):
    at: int
    is_live: bool
    receiver_lat: Optional[float] = None
    receiver_lon: Optional[float] = None
    aircraft: list[MapSnapshotAircraft]


# ---------------------------------------------------------------------------
# Stats — large nested aggregate. Stable sub-objects are typed; SQL-row
# collections stay list[dict]/dict passthroughs (see module docstring).
# ---------------------------------------------------------------------------

class SourceBreakdown(ApiModel):
    adsb: Optional[float] = None
    mlat: Optional[float] = None
    other: Optional[float] = None


class StatsTrends(ApiModel):
    flights_24h_prev: Optional[int] = None
    flights_7d_prev: Optional[int] = None


class StatsPreviousWindow(ApiModel):
    from_ts: Optional[int] = None
    to_ts: Optional[int] = None
    total_flights: Optional[int] = None
    total_positions: Optional[int] = None
    unique_aircraft: Optional[int] = None


class StatsLifetime(ApiModel):
    total_flights: Optional[int] = None
    total_positions: Optional[int] = None
    unique_aircraft: Optional[int] = None
    unique_airlines: Optional[int] = None
    oldest_flight: Optional[int] = None
    db_size_bytes: Optional[int] = None
    source_breakdown: Optional[SourceBreakdown] = None


class StatsResponse(ApiModel):
    total_flights: Optional[int] = None
    total_positions: Optional[int] = None
    unique_aircraft: Optional[int] = None
    unique_airlines: Optional[int] = None
    db_size_bytes: Optional[int] = None
    oldest_flight: Optional[int] = None
    flights_last_24h: Optional[int] = None
    flights_last_7d: Optional[int] = None
    source_breakdown: Optional[SourceBreakdown] = None
    top_airlines: list[dict] = Field(default_factory=list)
    top_aircraft_types: list[dict] = Field(default_factory=list)
    hourly_distribution: list[dict] = Field(default_factory=list)
    daily_unique_aircraft: list[dict] = Field(default_factory=list)
    altitude_distribution: list[dict] = Field(default_factory=list)
    military_flights: Optional[int] = None
    interesting_flights: Optional[int] = None
    anonymous_flights: Optional[int] = None
    # Emergency-squawk counts keyed by "7700"/"7600"/"7500" (non-identifier
    # keys) — passthrough dict.
    squawk_counts: dict = Field(default_factory=dict)
    # {"total": int, "items": [<flight rows>]} — passthrough.
    new_aircraft: dict = Field(default_factory=dict)
    # Full enriched flight row with first_seen renamed record_set_at, or null.
    furthest_aircraft: Optional[dict] = None
    receiver_lat: Optional[float] = None
    receiver_lon: Optional[float] = None
    trends: Optional[StatsTrends] = None
    previous_window: Optional[StatsPreviousWindow] = None
    lifetime: Optional[StatsLifetime] = None
    heatmap: list[dict] = Field(default_factory=list)
    top_countries: list[dict] = Field(default_factory=list)
    frequent_aircraft: list[dict] = Field(default_factory=list)
    top_routes: list[dict] = Field(default_factory=list)
    top_airports: list[dict] = Field(default_factory=list)
    # {"from": ts, "to": ts} ("from" is a Python keyword) or null — passthrough.
    range: Optional[dict] = None


# ---------------------------------------------------------------------------
# VDL2 / ACARS (opt-in feature)
# ---------------------------------------------------------------------------

class Vdl2FiledRoute(ApiModel):
    """Filed route parsed from a #M1BPOS /RP: block (dep/arr required)."""
    dep: str
    arr: str
    company_route: Optional[str] = None
    sid: Optional[str] = None
    star: Optional[str] = None
    approach: Optional[str] = None


class Vdl2Message(ApiModel):
    """One VDL2/ACARS message row (the `raw` JSON is excluded from list responses).
    extra="allow" keeps any SELECT column not named here, so the model can't drop
    fields — it documents the contract the SPA's Vdl2Message interface mirrors."""

    id: int
    ts: int
    icao_hex: Optional[str] = None
    registration: Optional[str] = None
    flight: Optional[str] = None
    label: Optional[str] = None
    mode: Optional[str] = None
    block_id: Optional[str] = None
    ack: Optional[str] = None
    msgno: Optional[str] = None
    freq: Optional[float] = None
    station_id: Optional[str] = None
    toaddr: Optional[str] = None
    dsta: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt: Optional[int] = None
    epu: Optional[float] = None
    app_name: Optional[str] = None
    app_ver: Optional[str] = None
    body: Optional[str] = None
    decoder: Optional[str] = None
    filed_route: Optional[Vdl2FiledRoute] = None


class Vdl2MessagesResponse(ApiModel):
    messages: list[Vdl2Message] = Field(default_factory=list)
    next_before_id: Optional[int] = None


class Vdl2TopLabel(ApiModel):
    label: Optional[str] = None
    messages: int = 0
    aircraft: int = 0


class Vdl2TopAirline(ApiModel):
    code: Optional[str] = None
    messages: int = 0
    name: Optional[str] = None


class Vdl2StatsResponse(ApiModel):
    total: int = 0
    last_hour: int = 0
    aircraft: int = 0
    top_labels: list[Vdl2TopLabel] = Field(default_factory=list)
    top_airlines: list[Vdl2TopAirline] = Field(default_factory=list)
    hourly: list[int] = Field(default_factory=list)
    # % of flights in the last 24h whose airframe also transmitted ACARS in the
    # flight window. Null when the cross-DB ATTACH is unavailable (don't fail the
    # card) — computed on the core history.db connection, not the vdl2.db one.
    flights_overlap_pct: Optional[float] = None



class Vdl2ActiveResponse(ApiModel):
    """ICAO hexes that transmitted ACARS in the last N minutes — for the map's
    'transmitting now' marker badge (client-side merge with the live snapshot)."""
    icao_hex: list[str] = Field(default_factory=list)
    count: int = 0


class Vdl2Position(ApiModel):
    lat: Optional[float] = None
    lon: Optional[float] = None
    icao_hex: Optional[str] = None
    ts: Optional[int] = None
    label: Optional[str] = None
    # True = precise (~0.001°) position parsed from a Label-16 AUTPOS body or a #M1BPOS body;
    # False = coarse (~0.1°) VDL2 XID link-frame fix from the lat/lon column.
    precise: Optional[bool] = None


class Vdl2PositionsResponse(ApiModel):
    """VDL2-derived positions for the optional map overlay. Sparse on an
    H1-dominated feed — only messages whose decoder emitted structured lat/lon."""
    points: list[Vdl2Position] = Field(default_factory=list)
    count: int = 0


class Vdl2TimeseriesResponse(ApiModel):
    """Bucketed VDL2 reception time-series for the Metrics page, in the same
    columnar shape as /api/metrics so the frontend chart builders are reused.
    Series values are normalized to msgs/min; `total` is the raw count in the
    window (the series must NOT be summed to get a count)."""
    bucket_seconds: int = 0
    metrics: list[str] = Field(default_factory=list)    # ["rate", "<freq>", ...]
    freqs: list[float] = Field(default_factory=list)    # top frequencies, same order as metrics[1:]
    total: int = 0
    newest_ts: Optional[int] = None
    newest_age_sec: Optional[int] = None
    data: list[list[float]] = Field(default_factory=list)  # [[ts...], [rate...], [freq1...], ...]


class Vdl2SignalResponse(ApiModel):
    """Per-frequency reception quality for the Metrics page: average signal level
    (dBFS) and SNR (dB) per channel, bucketed over the window. dumpvdl2-only — a
    vdlm2dec feed has no sig_level, so `metrics` is empty and the charts self-hide.
    Empty buckets are `null` (gaps), never 0. `metrics` (freq keys) indexes BOTH
    matrices: `signal[i+1]`/`snr[i+1]` align with `metrics[i]` (column 0 is ts)."""
    bucket_seconds: int = 0
    metrics: list[str] = Field(default_factory=list)    # ["<freq>", ...], shared by both matrices
    freqs: list[float] = Field(default_factory=list)
    samples: int = 0                                    # rows with a signal level in the window
    newest_ts: Optional[int] = None
    newest_age_sec: Optional[int] = None
    signal: list[list[Optional[float]]] = Field(default_factory=list)  # [[ts...], [sig_f1...], ...] dBFS
    snr: list[list[Optional[float]]] = Field(default_factory=list)     # [[ts...], [snr_f1...], ...] dB


class Vdl2OooiEvent(ApiModel):
    """One parsed OOOI (Out/Off/On/In) block-time report. Times are raw HHMM
    strings as transmitted; the frontend formats/compares them."""
    type: Optional[str] = None
    registration: Optional[str] = None
    flight: Optional[str] = None
    dep_icao: Optional[str] = None
    dest_icao: Optional[str] = None
    t_out: Optional[str] = None
    t_off: Optional[str] = None
    t_on: Optional[str] = None
    t_in: Optional[str] = None
    ts: Optional[int] = None


class Vdl2OooiSummary(ApiModel):
    """Flight-detail OOOI summary: the latest DEP + latest ARR parsed from the
    airframe's ACARS bodies in the flight window, plus a cheap `dsta`
    destination-airport fallback (from XID frames) when no OOOI body parses.
    EXPERIMENTAL — carrier-variant; commonly empty on an H1-dominated feed."""
    dep: Optional[Vdl2OooiEvent] = None
    arr: Optional[Vdl2OooiEvent] = None
    dsta: Optional[str] = None
    has_oooi: bool = False
