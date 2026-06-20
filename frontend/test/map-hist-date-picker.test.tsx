/**
 * MapHistDatePicker test (Audit 2026-06-20 coverage gap). The HIST date+time
 * chip was only mounted transitively by the smoke suite. We pin that it renders
 * both sub-pickers and that time selection propagates. The disabled-date range
 * (minSec/maxSec → react-day-picker `disabledMatcher`) is left to react-day-picker
 * + the seconds→Date conversion; asserting a specific disabled calendar cell is
 * brittle in jsdom (month navigation), so it's intentionally out of scope here.
 */

import type { ComponentProps } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen, within } from '@testing-library/react';
import { MapHistDatePicker } from '@/components/map/MapHistDatePicker';

type Props = ComponentProps<typeof MapHistDatePicker>;

function setup(overrides: Partial<Props> = {}) {
  const props: Props = {
    dateISO: '2026-06-15',
    timeHHMM: '09:30',
    onDateChange: vi.fn(),
    onTimeChange: vi.fn(),
    minSec: 1_700_000_000,
    maxSec: 1_800_000_000,
    ...overrides,
  };
  const utils = render(<MapHistDatePicker {...props} />);
  return { ...utils, props };
}

describe('MapHistDatePicker', () => {
  it('renders both the date and time sub-pickers', () => {
    setup();
    expect(screen.getByTestId('map-hist-date-picker')).toBeInTheDocument();
    expect(screen.getByTestId('map-hist-date')).toBeInTheDocument();
    expect(screen.getByTestId('map-hist-time')).toBeInTheDocument();
  });

  it('time selection propagates via onTimeChange', () => {
    const { props } = setup();
    fireEvent.click(screen.getByTestId('map-hist-time'));
    const popover = screen.getByTestId('time-picker-popover');
    fireEvent.click(within(popover).getByTestId('tp-h-14'));
    fireEvent.click(within(popover).getByTestId('tp-m-45'));
    expect(props.onTimeChange).toHaveBeenLastCalledWith('14:45');
  });
});
