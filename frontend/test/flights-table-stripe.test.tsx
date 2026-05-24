/**
 * v2.8.0 M8.4 — every flight row carries a 3 px left-border stripe whose
 * colour encodes `primary_source`. The stripe lives on the FIRST <td>
 * (timestamp cell), NOT on <tr>, because the underlying <table> uses
 * border-collapse (ui/Table.tsx) which suppresses tr-level borders.
 *
 * On mobile the Source column is hidden via `hideOnMobile`; the stripe is
 * the only visible source indicator. This test pins the colour-to-source
 * mapping so a future SourceBadge tweak can't silently re-key the stripe.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FlightsTable, type Flight } from '@/components/FlightsTable';

function flight(id: number, source: string | null): Flight {
  return {
    id,
    icao_hex: `aabb${id.toString().padStart(2, '0')}`,
    callsign: 'TEST',
    registration: 'X-TEST',
    aircraft_type: 'A320',
    type_desc: null,
    flags: 0,
    squawk: null,
    primary_source: source,
    first_seen: 1_700_000_000 + id,
    last_seen: 1_700_000_000 + id + 60,
    duration_sec: 60,
    max_alt_baro: 10000,
    max_gs: 400,
    max_distance_nm: 50,
    total_positions: 100,
    origin_icao: null,
    dest_icao: null,
  };
}

function renderTable(flights: Flight[]) {
  return render(
    <MemoryRouter>
      <FlightsTable
        flights={flights}
        isLoading={false}
        error={null}
        sortBy="first_seen"
        sortDir="desc"
        onSortChange={vi.fn()}
      />
    </MemoryRouter>,
  );
}

const VAR = {
  success: 'var(--color-success)',
  warn: 'var(--color-warn)',
  accent: 'var(--color-accent)',
  borderDefault: 'var(--color-border-default)',
};

describe('FlightsTable — source stripe (M8.4)', () => {
  it('ADS-B → success (green) on the first cell of the row', () => {
    const { getByTestId } = renderTable([flight(1, 'adsb')]);
    const stripeCell = getByTestId('flights-row-1-stripe');
    expect(stripeCell.style.borderLeftColor).toBe(VAR.success);
    expect(stripeCell.className).toContain('border-l-[3px]');
  });

  it('MLAT → warn (amber)', () => {
    const { getByTestId } = renderTable([flight(2, 'mlat')]);
    expect(getByTestId('flights-row-2-stripe').style.borderLeftColor).toBe(VAR.warn);
  });

  it('mixed → accent (blue)', () => {
    const { getByTestId } = renderTable([flight(3, 'mixed')]);
    expect(getByTestId('flights-row-3-stripe').style.borderLeftColor).toBe(VAR.accent);
  });

  it('other → border-default (effectively invisible)', () => {
    const { getByTestId } = renderTable([flight(4, 'other')]);
    expect(getByTestId('flights-row-4-stripe').style.borderLeftColor).toBe(VAR.borderDefault);
  });

  it('null source → border-default', () => {
    const { getByTestId } = renderTable([flight(5, null)]);
    expect(getByTestId('flights-row-5-stripe').style.borderLeftColor).toBe(VAR.borderDefault);
  });

  it('stripe lives on the FIRST <td>, not on <tr> (border-collapse guard)', () => {
    // Regression guard: if someone moves border-l-[3px] back onto <TR>,
    // border-collapse will silently swallow it and the stripe vanishes.
    const { getByTestId } = renderTable([flight(6, 'adsb')]);
    const row = getByTestId('flights-row-6');
    // The row itself MUST NOT carry border-l-[3px] — it'd be a no-op
    // under collapse mode but it'd be misleading.
    expect(row.className).not.toContain('border-l-[3px]');
    // The first cell carries it.
    const firstCell = within(row).getByTestId('flights-row-6-stripe');
    expect(firstCell.className).toContain('border-l-[3px]');
  });
});
