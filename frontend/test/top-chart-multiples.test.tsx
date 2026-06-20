/**
 * TopChartMultiples coverage (audit 2026-06-20 gap). Pins the loading-skeleton
 * vs six-cells branching; with no rankings each cell shows its empty state (the
 * EChart branch isn't reached, so this needs no chart mock).
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { TopChartMultiples } from '@/components/stats/TopChartMultiples';

describe('TopChartMultiples', () => {
  it('renders the skeleton grid while loading', () => {
    render(<MemoryRouter><TopChartMultiples loading={true} /></MemoryRouter>);
    expect(screen.getByTestId('stats-top-multiples')).toBeInTheDocument();
  });

  it('renders six ranking cells (empty-stated) when not loading with no data', () => {
    render(<MemoryRouter><TopChartMultiples loading={false} /></MemoryRouter>);
    expect(screen.getByTestId('stats-top-multiple-aircraft')).toBeInTheDocument();
    expect(screen.getByTestId('stats-top-multiple-routes')).toBeInTheDocument();
    // Six VIEWS, each with no rows → "No data.".
    expect(screen.getAllByText('No data.')).toHaveLength(6);
  });
});
