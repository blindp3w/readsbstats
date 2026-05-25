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
  it('renders the three flag pills with counts and any non-zero squawks', () => {
    // Sprint 1 #3: only non-zero squawks render. baseCounts has only 7700=1.
    renderStrip();
    expect(screen.getByTestId('flag-pill-military').textContent).toContain('12');
    expect(screen.getByTestId('flag-pill-interesting').textContent).toContain('4');
    expect(screen.getByTestId('flag-pill-anonymous').textContent).toContain('47');
    expect(screen.getByTestId('flag-pill-squawk-7700').textContent).toContain('1');
    // 7600 and 7500 are 0 — must NOT render.
    expect(screen.queryByTestId('flag-pill-squawk-7600')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flag-pill-squawk-7500')).not.toBeInTheDocument();
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

  it('hides all squawk pills when every squawk count is zero', () => {
    // Sprint 1 #3: dashboards treat empty state as silence (Datadog
    // convention). All-zero squawks should leave the strip showing only
    // the three flag pills, with no "0 emergencies" placeholder.
    renderStrip({
      military: 0,
      interesting: 0,
      anonymous: 0,
      squawks: { '7700': 0, '7600': 0, '7500': 0 },
    });
    expect(screen.queryByTestId('flag-pill-squawk-7700')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flag-pill-squawk-7600')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flag-pill-squawk-7500')).not.toBeInTheDocument();
  });

  it('hides all squawk pills when the squawks object is empty/missing', () => {
    renderStrip({
      military: 0,
      interesting: 0,
      anonymous: 0,
      squawks: {},
    });
    expect(screen.queryByTestId('flag-pill-squawk-7700')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flag-pill-squawk-7500')).not.toBeInTheDocument();
  });

  it('still renders Military / Interesting / Anonymous pills at count 0', () => {
    // The three flag types are "kinds of contacts" — 0 is informative
    // ("I've seen no military"), so they stay visible. Only emergency
    // squawks are hidden at 0.
    renderStrip({
      military: 0,
      interesting: 0,
      anonymous: 0,
      squawks: {},
    });
    expect(screen.getByTestId('flag-pill-military')).toBeInTheDocument();
    expect(screen.getByTestId('flag-pill-interesting')).toBeInTheDocument();
    expect(screen.getByTestId('flag-pill-anonymous')).toBeInTheDocument();
  });
});
