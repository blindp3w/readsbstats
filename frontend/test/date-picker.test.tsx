/**
 * DatePicker smoke test — opens the popover, picks a known day, asserts
 * onChange fires with the ISO date string. Guards the basic round-trip so
 * any future refactor of the react-day-picker version or props doesn't
 * silently break date selection on History / Stats / Metrics.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen, within } from '@testing-library/react';
import { DatePicker } from '@/components/ui/DatePicker';

function setup(value = '') {
  const onChange = vi.fn();
  const utils = render(
    <DatePicker
      value={value}
      onChange={onChange}
      data-testid="dp"
      ariaLabel="Pick a date"
    />,
  );
  return { ...utils, onChange };
}

describe('DatePicker', () => {
  it('renders placeholder text when value is empty', () => {
    setup('');
    expect(screen.getByTestId('dp')).toHaveTextContent('dd/mm/yyyy');
  });

  it('renders a formatted date when value is set', () => {
    setup('2026-05-18');
    // Locale-robust: the formatted string contains the year and a localized
    // representation of "May". We only assert the year is present.
    expect(screen.getByTestId('dp').textContent).toMatch(/2026/);
  });

  it('renders the placeholder for an impossible date instead of rolling over', () => {
    // Feb 31 must not silently become Mar 3 (audit 2026-06-15).
    setup('2026-02-31');
    expect(screen.getByTestId('dp')).toHaveTextContent('dd/mm/yyyy');
  });

  it('reopens when defaultOpen flips to true after mount', () => {
    // The map's HIST mode flips defaultOpen on a persistent instance; the
    // popover must follow (audit 2026-06-15).
    const onChange = vi.fn();
    const { rerender } = render(
      <DatePicker value="2026-05-15" onChange={onChange} data-testid="dp" defaultOpen={false} />,
    );
    expect(screen.queryByTestId('date-picker-popover')).toBeNull();
    rerender(
      <DatePicker value="2026-05-15" onChange={onChange} data-testid="dp" defaultOpen={true} />,
    );
    expect(screen.getByTestId('date-picker-popover')).toBeTruthy();
  });

  it('opens the popover and selects a day, calling onChange with ISO date', () => {
    const { onChange } = setup('2026-05-15');
    fireEvent.click(screen.getByTestId('dp'));
    // Popover renders in a portal — query screen-wide.
    const popover = screen.getByTestId('date-picker-popover');
    // react-day-picker labels day buttons with a locale-aware string (e.g.
    // "May 20th, 2026"). Match by visible text content inside a button.
    const day20 = within(popover)
      .getAllByRole('button')
      .find((b) => b.textContent?.trim() === '20');
    expect(day20).toBeDefined();
    fireEvent.click(day20!);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith('2026-05-20');
  });
});
