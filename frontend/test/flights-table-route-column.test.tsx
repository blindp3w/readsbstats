/**
 * Conditional ROUTE column — guards the audit-v2-2026-05-18 Issue E fix.
 *
 * When no flight in the current result set has origin/dest data, the column
 * is dropped entirely (instead of showing a column full of "—"). When at
 * least one row has route data, the column appears. Both the header (driven
 * by cols.map) and the body <TD> are guarded by the same `hasAnyRoute`
 * boolean — this test catches a future drift between the two.
 */

import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FlightsTable, type Flight } from '@/components/FlightsTable';

function makeFlight(overrides: Partial<Flight> = {}): Flight {
  return {
    id: 1,
    icao_hex: '484abc',
    callsign: 'TEST1',
    registration: 'SP-TEST',
    aircraft_type: 'B738',
    flags: 0,
    primary_source: 'adsb_icao',
    first_seen: 1_700_000_000,
    last_seen: 1_700_001_000,
    duration_sec: 1000,
    max_alt_baro: 35000,
    max_gs: 460,
    max_distance_nm: 120,
    total_positions: 200,
    origin_icao: null,
    dest_icao: null,
    ...overrides,
  };
}

function renderWith(flights: Flight[]) {
  return render(
    <MemoryRouter>
      <FlightsTable
        flights={flights}
        isLoading={false}
        error={null}
        sortBy="first_seen"
        sortDir="desc"
        onSortChange={() => {}}
      />
    </MemoryRouter>,
  );
}

describe('FlightsTable ROUTE column', () => {
  it('shows ROUTE header when any flight has origin/dest', () => {
    const { queryByText } = renderWith([
      makeFlight({ id: 1, origin_icao: 'EPWA', dest_icao: 'EGLL' }),
      makeFlight({ id: 2, origin_icao: null, dest_icao: null }),
    ]);
    expect(queryByText('Route')).not.toBeNull();
  });

  it('hides ROUTE header when no flight has route data', () => {
    const { queryByText } = renderWith([
      makeFlight({ id: 1, origin_icao: null, dest_icao: null }),
      makeFlight({ id: 2, origin_icao: null, dest_icao: null }),
    ]);
    expect(queryByText('Route')).toBeNull();
  });

  it('renders the route cell content when the column is shown', () => {
    const { queryByText } = renderWith([
      makeFlight({ id: 1, origin_icao: 'EPWA', dest_icao: 'EGLL' }),
    ]);
    expect(queryByText('EPWA→EGLL')).not.toBeNull();
  });
});
