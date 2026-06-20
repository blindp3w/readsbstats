/**
 * MapModeControl coverage (audit 2026-06-20 gap). Pins that selecting a segment
 * emits the new mode (the onValueChange null-guard drops Radix's deselect "").
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MapModeControl } from '@/components/map/MapModeControl';

describe('MapModeControl', () => {
  it('emits the selected mode via onChange', () => {
    const onChange = vi.fn();
    render(<MapModeControl mode="live" onChange={onChange} />);
    fireEvent.click(screen.getByTestId('map-mode-rewind'));
    expect(onChange).toHaveBeenCalledWith('rewind');
    fireEvent.click(screen.getByTestId('map-mode-hist'));
    expect(onChange).toHaveBeenLastCalledWith('hist');
  });

  it('drops Radix deselect ("") via the null-guard', () => {
    const onChange = vi.fn();
    render(<MapModeControl mode="live" onChange={onChange} />);
    // Clicking the already-active segment makes Radix emit onValueChange("");
    // the `if (!v) return` guard must swallow it so the parent never receives
    // an invalid empty mode. (Without the guard, onChange fires with "".)
    fireEvent.click(screen.getByTestId('map-mode-live'));
    expect(onChange).not.toHaveBeenCalled();
  });
});
