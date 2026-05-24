import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { KpiSparkline } from '@/components/stats/KpiSparkline';

describe('KpiSparkline', () => {
  it('renders nothing when data.length < 7', () => {
    const { container } = render(<KpiSparkline data={[1, 2, 3]} />);
    expect(container.querySelector('svg')).toBeNull();
  });

  it('renders nothing for an empty data array', () => {
    const { container } = render(<KpiSparkline data={[]} />);
    expect(container.querySelector('svg')).toBeNull();
  });

  it('renders an svg + polyline when data.length >= 7', () => {
    render(<KpiSparkline data={[1, 2, 3, 4, 5, 6, 7]} />);
    const svg = screen.getByTestId('kpi-sparkline');
    expect(svg.tagName.toLowerCase()).toBe('svg');
    const poly = svg.querySelector('polyline');
    expect(poly).not.toBeNull();
    const pts = poly!.getAttribute('points');
    expect(pts).toBeTruthy();
    // 7 points → at least 7 coordinate pairs separated by spaces
    expect(pts!.trim().split(/\s+/).length).toBe(7);
  });

  it('handles a flat series without crashing (range=0 protected)', () => {
    render(<KpiSparkline data={[5, 5, 5, 5, 5, 5, 5]} />);
    const svg = screen.getByTestId('kpi-sparkline');
    expect(svg.querySelector('polyline')).not.toBeNull();
  });

  it('sets a role and aria-label for screen readers', () => {
    render(<KpiSparkline data={[1, 2, 3, 4, 5, 6, 7]} ariaLabel="Flights trend" />);
    const svg = screen.getByTestId('kpi-sparkline');
    expect(svg.getAttribute('role')).toBe('img');
    expect(svg.getAttribute('aria-label')).toBe('Flights trend');
  });
});
