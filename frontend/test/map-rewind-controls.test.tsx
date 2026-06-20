/**
 * MapRewindControls interaction test (Audit 2026-06-20 coverage gap). Row 2 of
 * the map command bar only renders in HIST/rewind mode, so the smoke suite (live
 * mode) never exercised the seek sign convention, the speed group, or the
 * React-Compiler slider DOM-setter workaround.
 */

import type { ComponentProps } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import { MapRewindControls } from '@/components/map/MapRewindControls';

type Props = ComponentProps<typeof MapRewindControls>;

function setup(overrides: Partial<Props> = {}) {
  const props: Props = {
    scrubMin: 0,
    scrubMax: 1000,
    scrubValue: 100,
    onScrubChange: vi.fn(),
    onSeek: vi.fn(),
    onJumpNow: vi.fn(),
    label: '1h 0m ago',
    playing: false,
    onPlayToggle: vi.fn(),
    speed: 1,
    onSpeedChange: vi.fn(),
    ...overrides,
  };
  const utils = render(<MapRewindControls {...props} />);
  return { ...utils, props };
}

describe('MapRewindControls', () => {
  it('seek buttons fire onSeek with the documented sign convention (+sec = back)', () => {
    const { props } = setup();
    fireEvent.click(screen.getByTestId('map-jump-back-1h'));
    fireEvent.click(screen.getByTestId('map-jump-back-10m'));
    fireEvent.click(screen.getByTestId('map-jump-fwd-10m'));
    fireEvent.click(screen.getByTestId('map-jump-fwd-1h'));
    expect(vi.mocked(props.onSeek).mock.calls.map((c) => c[0]))
      .toEqual([3600, 600, -600, -3600]);
  });

  it('play / now / speed controls fire their callbacks; speed reflects aria-pressed', () => {
    const { props } = setup({ speed: 2 });
    fireEvent.click(screen.getByTestId('map-play-toggle'));
    expect(props.onPlayToggle).toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('map-jump-now'));
    expect(props.onJumpNow).toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('map-speed-5x'));
    expect(props.onSpeedChange).toHaveBeenCalledWith(5);
    expect(screen.getByTestId('map-speed-2x')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('map-speed-5x')).toHaveAttribute('aria-pressed', 'false');
  });

  it('slider DOM value tracks scrubValue across rerenders', () => {
    const { rerender, props } = setup({ scrubValue: 100 });
    const slider = screen.getByTestId('map-rewind-slider') as HTMLInputElement;
    expect(slider.value).toBe('100');
    rerender(<MapRewindControls {...props} scrubValue={250} />);
    expect(slider.value).toBe('250');
  });
});
