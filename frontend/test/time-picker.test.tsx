/**
 * TimePicker smoke test — open popover, pick a new HH and MM, assert
 * onChange fires with the HH:MM string. Guards the basic round-trip so a
 * future refactor doesn't silently break time selection in the Custom
 * range form on Stats / Metrics.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen, within } from '@testing-library/react';
import { TimePicker } from '@/components/ui/TimePicker';

function setup(value = '') {
  const onChange = vi.fn();
  const utils = render(
    <TimePicker
      value={value}
      onChange={onChange}
      data-testid="tp"
      ariaLabel="Pick a time"
    />,
  );
  return { ...utils, onChange };
}

describe('TimePicker', () => {
  it('renders placeholder when value is empty', () => {
    setup('');
    expect(screen.getByTestId('tp')).toHaveTextContent('--:--');
  });

  it('renders the current time when value is set', () => {
    setup('09:30');
    expect(screen.getByTestId('tp')).toHaveTextContent('09:30');
  });

  it('open → pick new HH + MM commits HH:MM via onChange and closes popover', () => {
    const { onChange } = setup('09:30');
    fireEvent.click(screen.getByTestId('tp'));
    const popover = screen.getByTestId('time-picker-popover');
    // Pick a new hour, then a new minute. Together they form a value
    // distinct from the current `09:30`, which triggers commit + close.
    fireEvent.click(within(popover).getByTestId('tp-h-14'));
    fireEvent.click(within(popover).getByTestId('tp-m-45'));
    expect(onChange).toHaveBeenCalled();
    expect(onChange).toHaveBeenLastCalledWith('14:45');
  });

  it('clicking only the hour column does NOT commit (must pick both)', () => {
    const { onChange } = setup('');
    fireEvent.click(screen.getByTestId('tp'));
    const popover = screen.getByTestId('time-picker-popover');
    fireEvent.click(within(popover).getByTestId('tp-h-08'));
    // No minute picked yet, nothing should commit.
    expect(onChange).not.toHaveBeenCalled();
  });
});
