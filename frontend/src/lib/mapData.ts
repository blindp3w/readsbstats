// Pure data transforms for the live map, extracted so they're unit-testable
// (LiveMap itself is mocked to null in jsdom — see test/setup.ts).

// Keep only the freshest (highest `ts`) row per icao_hex.
//
// The /api/map/snapshot endpoint groups by flight_id, which is correct for
// ordinary live polling. But during Rewind / HIST an airframe can legitimately
// appear under two flight_ids inside the 600s snapshot window (the collector
// closed one flight and opened another for the same icao_hex). Rendering both
// would double-plot the aircraft at two slightly different positions; this
// collapses them to the most recent fix.
//
// Generic over the minimal shape so it doesn't couple to LiveMap's Aircraft.
export function dedupeFreshestByIcao<T extends { icao_hex: string; ts: number }>(
  rows: readonly T[],
): T[] {
  const byIcao: Record<string, T> = {};
  for (const r of rows) {
    const prev = byIcao[r.icao_hex];
    if (!prev || r.ts > prev.ts) byIcao[r.icao_hex] = r;
  }
  return Object.values(byIcao);
}
