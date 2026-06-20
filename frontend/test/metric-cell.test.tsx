/**
 * MetricCell aria-label coverage (audit 2026-06-20 gap). The JSX-value →
 * valueText fallback for the accessible name is the only non-trivial logic.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MetricCell } from '@/components/flight/MetricCell';

describe('MetricCell', () => {
  it('uses valueText for the aria-label when value is JSX', () => {
    render(<MetricCell label="Window" value={<span>a–b</span>} valueText="a to b" testid="mc" />);
    expect(screen.getByTestId('mc')).toHaveAttribute('aria-label', 'Window a to b');
  });

  it('falls back to a string value when no valueText is given', () => {
    render(<MetricCell label="Alt" value="35000 ft" testid="mc" />);
    expect(screen.getByTestId('mc')).toHaveAttribute('aria-label', 'Alt 35000 ft');
  });

  it('includes a string sublabel in the aria-label', () => {
    render(<MetricCell label="Max" value="500" sublabel="at 12:00" testid="mc" />);
    expect(screen.getByTestId('mc')).toHaveAttribute('aria-label', 'Max 500 — at 12:00');
  });
});
