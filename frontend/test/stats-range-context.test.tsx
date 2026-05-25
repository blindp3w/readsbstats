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

  it('drops the time portion entirely for ≥1-day windows (30d)', () => {
    // Sprint 1 #2: a 30d window doesn't need HH:MM precision in the
    // context line — date-only is the right level for the operator.
    const state: RangeState = {
      value: '30d',
      from: 1_700_000_000,
      to: 1_702_592_000,
    };
    render(<RangeContextLine state={state} />);
    const ctx = screen.getByTestId('stats-range-context');
    // No colon (no HH:MM:SS) in the range tail.
    const rangeTail = ctx.textContent?.split('·')[1] ?? '';
    expect(rangeTail).not.toMatch(/\d{1,2}:\d{2}/);
  });

  it('keeps minute precision (no seconds) for 24h window', () => {
    const state: RangeState = {
      value: '24h',
      from: 1_700_000_000,
      to: 1_700_086_400,
    };
    render(<RangeContextLine state={state} />);
    const ctx = screen.getByTestId('stats-range-context');
    // Seconds are stripped everywhere; HH:MM stays for 24h.
    expect(ctx.textContent).not.toMatch(/\d{1,2}:\d{2}:\d{2}/);
    expect(ctx.textContent).toMatch(/\d{1,2}:\d{2}/);
  });

  it('uses date-only for a custom range spanning ≥1 day', () => {
    const state: RangeState = {
      value: 'custom',
      from: 1_700_000_000,
      to: 1_700_000_000 + 5 * 86400,
    };
    render(<RangeContextLine state={state} />);
    const ctx = screen.getByTestId('stats-range-context');
    const rangeTail = ctx.textContent?.split('·')[1] ?? '';
    expect(rangeTail).not.toMatch(/\d{1,2}:\d{2}/);
  });

  it('keeps time format for a sub-day custom range', () => {
    const state: RangeState = {
      value: 'custom',
      from: 1_700_000_000,
      to: 1_700_000_000 + 5 * 3600, // 5h
    };
    render(<RangeContextLine state={state} />);
    const ctx = screen.getByTestId('stats-range-context');
    expect(ctx.textContent).toMatch(/\d{1,2}:\d{2}/);
  });
});
