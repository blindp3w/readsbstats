import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { FlagBadgeStrip, type FlagCounts } from '@/components/stats/FlagBadgeStrip';

const baseCounts: FlagCounts = {
  military: 12,
  interesting: 4,
  anonymous: 47,
  squawks: { '7700': 1, '7600': 0, '7500': 0 },
};

function renderStrip(counts: FlagCounts = baseCounts) {
  return render(
    <MemoryRouter>
      <FlagBadgeStrip counts={counts} />
    </MemoryRouter>,
  );
}

describe('FlagBadgeStrip', () => {
  it('renders all six pills with counts', () => {
    renderStrip();
    expect(screen.getByTestId('flag-pill-military').textContent).toContain('12');
    expect(screen.getByTestId('flag-pill-interesting').textContent).toContain('4');
    expect(screen.getByTestId('flag-pill-anonymous').textContent).toContain('47');
    expect(screen.getByTestId('flag-pill-squawk-7700').textContent).toContain('1');
    expect(screen.getByTestId('flag-pill-squawk-7600').textContent).toContain('0');
    expect(screen.getByTestId('flag-pill-squawk-7500').textContent).toContain('0');
  });

  it('squawk pills link to the right /history filter URL', () => {
    renderStrip();
    const pill = screen.getByTestId('flag-pill-squawk-7700');
    expect(pill.getAttribute('href')).toBe('/history?squawk=7700');
  });

  it('flag pills link to the right /history filter URL', () => {
    renderStrip();
    expect(screen.getByTestId('flag-pill-military').getAttribute('href')).toBe(
      '/history?flags=military',
    );
    expect(screen.getByTestId('flag-pill-anonymous').getAttribute('href')).toBe(
      '/history?flags=anonymous',
    );
  });

  it('exposes accessible aria-labels mentioning the count', () => {
    renderStrip();
    expect(screen.getByTestId('flag-pill-military').getAttribute('aria-label')).toMatch(
      /12 military/i,
    );
    expect(screen.getByTestId('flag-pill-squawk-7700').getAttribute('aria-label')).toMatch(
      /1 squawk 7700/i,
    );
  });

  it('falls back to 0 when a squawk code is missing from the payload', () => {
    renderStrip({
      military: 0,
      interesting: 0,
      anonymous: 0,
      squawks: {},
    });
    expect(screen.getByTestId('flag-pill-squawk-7500').textContent).toContain('0');
  });
});
