import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WindsAloftCard } from '@/components/flight/WindsAloftCard';
import { useUnitsStore } from '@/store/units';
import type { Vdl2Position } from '@/lib/types';

const fix = (ts: number, extra: Partial<Vdl2Position> = {}): Vdl2Position => ({
  lat: 52,
  lon: 21,
  icao_hex: '4ca123',
  ts,
  label: 'H1',
  precise: true,
  source: 'h1-abs',
  ...extra,
});

beforeEach(() => useUnitsStore.setState({ units: 'aeronautical' }));

describe('WindsAloftCard', () => {
  it('renders nothing when no fix carries temperature or wind', () => {
    const { container } = render(<WindsAloftCard fixes={[fix(1), fix(2, { alt: 30000 })]} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing for an empty list', () => {
    const { container } = render(<WindsAloftCard fixes={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it('lists atmospheric fixes oldest first with alt, temp and wind', () => {
    render(
      <WindsAloftCard
        fixes={[
          fix(2000, { alt: 36000, temp_c: -54, wind_dir: 280, wind_kt: 61 }),
          fix(1000, { alt: 24000, temp_c: -30.5, wind_dir: 5, wind_kt: 12 }),
          fix(1500), // no atmospherics → excluded
        ]}
      />,
    );
    const rows = screen.getAllByTestId('winds-aloft-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('24,000 ft');
    expect(rows[0].textContent).toContain('-30.5');
    expect(rows[0].textContent).toContain('005°');
    expect(rows[0].textContent).toContain('12 kt');
    expect(rows[1].textContent).toContain('36,000 ft');
    expect(rows[1].textContent).toContain('280°');
    expect(screen.getByTestId('flight-winds-card').textContent).toContain('Winds');
  });

  it('shows a dash for the missing half of a partial fix', () => {
    render(<WindsAloftCard fixes={[fix(1, { temp_c: -40 })]} />);
    const cells = screen.getByTestId('winds-aloft-row').querySelectorAll('td');
    expect(cells[1].textContent).toBe('—'); // alt
    expect(cells[2].textContent).toContain('-40');
    expect(cells[3].textContent).toBe('—'); // wind
  });

  it('wraps the table in a horizontally scrollable container (mobile)', () => {
    render(<WindsAloftCard fixes={[fix(1, { temp_c: -40, alt: 1000 })]} />);
    expect(screen.getByRole('table').parentElement?.className).toContain('overflow-x-auto');
  });
});
