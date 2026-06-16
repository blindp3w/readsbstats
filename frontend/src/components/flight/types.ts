// Shared types for the Flight detail surfaces. Extracted verbatim from
// pages/Flight.tsx so the per-component modules (FlightHeader, PositionTable)
// and the page consume one canonical definition.

export interface Position {
  ts: number;
  lat: number | null;
  lon: number | null;
  alt_baro: number | null;
  alt_geom: number | null;
  gs: number | null;
  track: number | null;
  baro_rate: number | null;
  rssi: number | null;
  source_type: string | null;
}

export interface OtherFlight {
  id: number;
  callsign: string | null;
  first_seen: number;
  duration_sec: number;
  primary_source: string | null;
  origin_icao: string | null;
  dest_icao: string | null;
}

export interface FlightDetail {
  flight: {
    id: number;
    icao_hex: string;
    callsign: string | null;
    registration: string | null;
    aircraft_type: string | null;
    type_desc: string | null;
    flags: number;
    squawk: string | null;
    primary_source: string | null;
    first_seen: number;
    last_seen: number;
    duration_sec: number;
    max_alt_baro: number | null;
    max_gs: number | null;
    max_distance_nm: number | null;
    total_positions: number;
    adsb_positions: number;
    mlat_positions: number;
    origin_icao: string | null;
    dest_icao: string | null;
    origin_name: string | null;
    dest_name: string | null;
    airline_name: string | null;
  };
  other_flights: OtherFlight[];
  receiver_lat: number | null;
  receiver_lon: number | null;
}

export interface PhotoResp {
  thumbnail_url: string | null;
  large_url: string | null;
  link_url: string | null;
  photographer: string | null;
  is_type_photo: boolean;
}

// At-max position lookups for the M3.1 header sub-labels. Computed
// client-side from `positions` (NOT via equality against the flight-level
// aggregates) because `max_gs` is REAL and `max_distance_nm` requires
// per-position haversine. Single pass, O(n).
export interface AtMax {
  altRate: number | null;
  speedTrack: number | null;
  distBearing: number | null;
}
