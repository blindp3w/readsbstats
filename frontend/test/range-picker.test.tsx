// components/RangePicker.tsx — unit-level twin of the CI Playwright lock
// `test_v2_range_picker_reopen_after_preset_reflects_new_window`.
//
// Contracts pinned here:
//  - preset clicks emit the RangeValue through onPreset;
//  - the Custom form pre-fills from the CURRENT resolved window;
//  - Apply round-trips the window back through onCustom as epochs;
//  - the `key={from-to}` remount contract: an external range change
//    re-initialises the form (no stale window on reopen);
//  - From >= To is rejected with a visible error, not a silent apply.
//
// Assertions use the TimePicker trigger text (exactly the "HH:MM" value) to
// stay independent of the DatePicker's display date format.

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { RangePicker, type RangeState } from '@/components/RangePicker';

const epoch = (y: number, mo: number, d: number, h: number, mi: number) =>
  Math.floor(new Date(y, mo, d, h, mi).getTime() / 1000);

const FROM = epoch(2026, 0, 10, 10, 0);
const TO = epoch(2026, 0, 17, 14, 30);

function renderPicker(stateOver: Partial<RangeState> = {}, props: object = {}) {
  const onPreset = vi.fn();
  const onCustom = vi.fn();
  const state: RangeState = { value: '7d', from: FROM, to: TO, ...stateOver };
  const utils = render(
    <RangePicker state={state} onPreset={onPreset} onCustom={onCustom} {...props} />,
  );
  return { onPreset, onCustom, state, ...utils };
}

describe('RangePicker presets', () => {
  it('emits the preset value through onPreset', () => {
    const { onPreset } = renderPicker();
    fireEvent.click(screen.getByTestId('range-30d'));
    expect(onPreset).toHaveBeenCalledWith('30d');
  });

  it('hides the All preset when allowAll is false', () => {
    renderPicker({}, { allowAll: false });
    expect(screen.queryByTestId('range-all')).toBeNull();
    expect(screen.getByTestId('range-90d')).toBeInTheDocument();
  });
});

describe('RangePicker custom form', () => {
  it('pre-fills From/To from the current resolved window', () => {
    renderPicker();
    fireEvent.click(screen.getByTestId('range-custom-toggle'));
    expect(screen.getByTestId('range-custom-from-time')).toHaveTextContent('10:00');
    expect(screen.getByTestId('range-custom-to-time')).toHaveTextContent('14:30');
  });

  it('Apply round-trips the pre-filled window through onCustom as epochs', () => {
    const { onCustom } = renderPicker();
    fireEvent.click(screen.getByTestId('range-custom-toggle'));
    fireEvent.click(screen.getByTestId('range-custom-apply'));
    expect(onCustom).toHaveBeenCalledWith(FROM, TO);
  });

  it('re-initialises the form when the external range changes (key remount)', () => {
    // The Playwright lock scenario: a preset click while the popover state
    // is live must not leave the form showing the previous window.
    const { rerender, onPreset, onCustom } = renderPicker();
    fireEvent.click(screen.getByTestId('range-custom-toggle'));
    expect(screen.getByTestId('range-custom-from-time')).toHaveTextContent('10:00');

    const newFrom = epoch(2026, 1, 1, 12, 30);
    const newTo = epoch(2026, 1, 8, 9, 15);
    rerender(
      <RangePicker
        state={{ value: '30d', from: newFrom, to: newTo }}
        onPreset={onPreset}
        onCustom={onCustom}
      />,
    );
    expect(screen.getByTestId('range-custom-from-time')).toHaveTextContent('12:30');
    expect(screen.getByTestId('range-custom-to-time')).toHaveTextContent('09:15');
  });

  it('rejects From >= To with a visible error and no onCustom call', () => {
    const { onCustom } = renderPicker({
      from: epoch(2026, 0, 17, 14, 30),
      to: epoch(2026, 0, 10, 10, 0),
    });
    fireEvent.click(screen.getByTestId('range-custom-toggle'));
    fireEvent.click(screen.getByTestId('range-custom-apply'));
    expect(screen.getByRole('alert')).toHaveTextContent('From must be earlier than To.');
    expect(onCustom).not.toHaveBeenCalled();
  });
});
