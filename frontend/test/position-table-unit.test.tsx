/**
 * Direct-render unit test for the extracted PositionTable component
 * (src/components/flight/PositionTable.tsx). Full-page coverage of the
 * sampling/RSSI/footer behaviour lives in position-table-*.test.tsx; this
 * asserts the component renders standalone with a minimal fixture so the
 * extraction is independently exercised.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { PositionTable } from '@/components/flight/PositionTable';
import type { Position } from '@/components/flight/types';

const POSITIONS: Position[] = [
  {
    ts: 1_700_000_000,
    lat: 52.0,
    lon: 21.0,
    alt_baro: 10000,
    alt_geom: null,
    gs: 250,
    track: 90,
    baro_rate: null,
    rssi: -10,
    source_type: 'adsb_icao',
  },
  {
    ts: 1_700_000_060,
    lat: 52.01,
    lon: 21.01,
    alt_baro: 11000,
    alt_geom: null,
    gs: 255,
    track: 90,
    baro_rate: null,
    rssi: -8,
    source_type: 'mlat',
  },
];

beforeEach(() => {
  globalThis.localStorage.clear();
});

function renderTable() {
  return render(
    <TooltipProvider delayDuration={0}>
      <PositionTable positions={POSITIONS} total={POSITIONS.length} loading={false} />
    </TooltipProvider>,
  );
}

describe('PositionTable (direct render)', () => {
  it('renders one row per position with the source stripe + rssi cell', async () => {
    const { container } = renderTable();
    await waitFor(() => {
      const rows = container.querySelectorAll('[data-testid^="flight-position-row-"]');
      if (rows.length === 0) throw new Error('rows not ready');
    });
    const rows = container.querySelectorAll('[data-testid^="flight-position-row-"]');
    expect(rows.length).toBe(2);
    // First row is adsb_icao → success (green) stripe on its first cell.
    const firstCell = rows[0].querySelector('td');
    expect(firstCell?.style.borderLeftColor).toBe('var(--color-success)');
    // RSSI cells render one per row.
    expect(container.querySelectorAll('[data-testid="rssi-cell"]').length).toBe(2);
  });
});
