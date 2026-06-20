/**
 * useMapPlaybackState transition coverage (audit 2026-06-20 gap). The pure
 * mapTime helpers were tested, but the hook's mode transitions + seek direction
 * weren't. Catch-up→live (interval-driven) is left out — it needs fake timers
 * and is lower-value than the transition contract here.
 */
import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useMapPlaybackState } from '@/hooks/useMapPlaybackState';

const MAX_REWIND = 86400; // 1 day

describe('useMapPlaybackState', () => {
  it('handleModeChange switches modes; onJumpNow returns to live', () => {
    const { result } = renderHook(() => useMapPlaybackState(MAX_REWIND));
    expect(result.current.mode).toBe('live');
    act(() => result.current.handleModeChange('rewind'));
    expect(result.current.mode).toBe('rewind');
    act(() => result.current.handleModeChange('hist'));
    expect(result.current.mode).toBe('hist');
    act(() => result.current.onJumpNow());
    expect(result.current.mode).toBe('live');
  });

  it('onSeek in rewind: delta>0 goes back (offset grows), delta<0 advances; clamped to [0, max]', () => {
    const { result } = renderHook(() => useMapPlaybackState(MAX_REWIND));
    act(() => result.current.handleModeChange('rewind'));
    act(() => result.current.onSeek(600));
    expect(result.current.rewindOffsetSec).toBe(600);
    act(() => result.current.onSeek(-600));
    expect(result.current.rewindOffsetSec).toBe(0);
    act(() => result.current.onSeek(-600));   // clamp at 0 (lower bound)
    expect(result.current.rewindOffsetSec).toBe(0);
    act(() => result.current.onSeek(MAX_REWIND + 600));  // clamp at max (upper bound)
    expect(result.current.rewindOffsetSec).toBe(MAX_REWIND);
  });
});
