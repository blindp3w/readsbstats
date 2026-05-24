import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RangeContextLine } from '@/components/stats/RangeContextLine';
import type { RangeState } from '@/components/RangePicker';

describe('RangeContextLine', () => {
  it('renders the preset title and "compared with previous 24h" for 24h', () => {
    const state: RangeState = {
      value: '24h',
      from: 1_700_000_000,
      to: 1_700_086_400,
    };
    render(<RangeContextLine state={state} />);
    const ctx = screen.getByTestId('stats-range-context');
    expect(ctx.textContent).toContain('last 24 hours');
    expect(ctx.textContent).toContain('previous 24h');
  });

  it('omits the "compared with" phrase for windows without a baseline (30d)', () => {
    const state: RangeState = {
      value: '30d',
      from: 1_700_000_000,
      to: 1_702_592_000,
    };
    render(<RangeContextLine state={state} />);
    const ctx = screen.getByTestId('stats-range-context');
    expect(ctx.textContent).toContain('last 30 days');
    expect(ctx.textContent).not.toContain('compared with');
  });

  it('renders the from→to range when present', () => {
    const state: RangeState = {
      value: 'custom',
      from: 1_700_000_000,
      to: 1_700_086_400,
    };
    render(<RangeContextLine state={state} />);
    const ctx = screen.getByTestId('stats-range-context');
    expect(ctx.textContent).toContain('→');
  });

  it('renders a refreshing spinner when isFetching is true', () => {
    const state: RangeState = { value: 'all' };
    const { container, rerender } = render(<RangeContextLine state={state} />);
    expect(container.textContent).not.toContain('refreshing');
    rerender(<RangeContextLine state={state} isFetching />);
    expect(container.textContent).toContain('refreshing');
  });
});
