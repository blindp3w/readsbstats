// ECharts option builder + StatsResponse shape for the Stats page.
// Lives in its own file so the page only exports the component — required
// by `react-refresh/only-export-components`. The response shape is also
// consumed by `components/charts/topRows.ts` for the rankings rows.

import type { EChartsOption } from 'echarts';
import { CHART_COLORS, baseOption, valueAxis } from '@/components/charts/theme';

export interface StatsResponse {
  total_flights: number;
  total_positions: number;
  unique_aircraft: number;
  unique_airlines: number;
  db_size_bytes: number | null;
  oldest_flight: number | null;
  flights_last_24h: number;
  flights_last_7d: number;
  source_breakdown: { adsb: number; mlat: number; other: number };
  top_airlines: { airline: string; airline_name: string | null; flights: number }[];
  // SQL aliases the column to `type`, not `aircraft_type`.
  top_aircraft_types: { type: string; type_desc: string | null; flights: number }[];
  hourly_distribution: { hour: number; count: number }[];
  // SQL returns `unique_aircraft`, not `unique`. Ordered ASC by day.
  daily_unique_aircraft: { day: string; unique_aircraft: number; flights: number }[];
  altitude_distribution: { band: string; count: number }[];
  military_flights: number;
  interesting_flights: number;
  anonymous_flights: number;
  heatmap: { dow: number; hour: number; count: number }[];
  top_countries: { country: string; flights: number }[];
  trends?: { flights_24h_prev: number; flights_7d_prev: number };
  // Totals for the period of equal length immediately preceding the
  // requested window. Backend returns this only when `from`/`to` are
  // supplied; unfiltered (all-time) requests get `null`. Drives the
  // delta chip on every numeric KPI card.
  previous_window?: {
    from_ts: number;
    to_ts: number;
    total_flights: number;
    total_positions: number;
    unique_aircraft: number;
  } | null;
  frequent_aircraft: {
    icao_hex: string;
    registration: string | null;
    aircraft_type: string | null;
    flights: number;
  }[];
  top_routes?: { origin_icao: string; dest_icao: string; flights: number }[];
  top_airports?: {
    icao_code: string;
    name?: string | null;
    appearances?: number;
    flights?: number;
  }[];
  new_aircraft?: {
    total: number;
    items: {
      icao_hex: string;
      registration: string | null;
      aircraft_type: string | null;
      type_desc: string | null;
      flags?: number;
      first_seen_ever?: number;
    }[];
  };
  squawk_counts?: { '7700'?: number; '7600'?: number; '7500'?: number };
  // Window-scoped furthest flight. Backend returns the full flight row,
  // or null for empty windows. `record_set_at` is the `first_seen` of
  // the record-holding flight (when the flight started) — used by
  // MaxRangeCard for the "set {date}" sublabel.
  furthest_aircraft?: {
    icao_hex: string;
    callsign: string | null;
    registration?: string | null;
    aircraft_type: string | null;
    type_desc: string | null;
    max_distance_nm: number | null;
    record_set_at?: number | null;
  } | null;
  // Lifetime block — receiver-wide totals that DO NOT change when the
  // user picks a window. Consumed by the "About this receiver" footer.
  // Always present; the same values as the top-level fields when the
  // request is unfiltered.
  lifetime?: {
    total_flights: number;
    total_positions: number;
    unique_aircraft: number;
    unique_airlines: number;
    oldest_flight: number | null;
    db_size_bytes: number | null;
    source_breakdown: { adsb: number; mlat: number; other: number };
  };
}

export function buildBarOption(
  data: Array<Record<string, string | number>>,
  xKey: string,
  yKey: string,
): EChartsOption {
  return {
    ...baseOption(),
    xAxis: {
      type: 'category',
      data: data.map((d) => String(d[xKey])),
      axisLine: { lineStyle: { color: CHART_COLORS.grid } },
      axisLabel: { color: CHART_COLORS.textDim },
    },
    yAxis: valueAxis(),
    series: [
      {
        type: 'bar',
        data: data.map((d) => Number(d[yKey] ?? 0)),
        itemStyle: { color: CHART_COLORS.accent, borderRadius: [3, 3, 0, 0] },
      },
    ],
  };
}
