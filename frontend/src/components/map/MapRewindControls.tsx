import { useEffect, useRef } from 'react';
import { PlayIcon, PauseIcon } from '@radix-ui/react-icons';
import { cn } from '@/lib/cn';

export type PlaybackSpeed = 1 | 2 | 5 | 10;
export const PLAYBACK_SPEEDS: PlaybackSpeed[] = [1, 2, 5, 10];

interface Props {
  // Scrubber bounds + value in a generic unit (seconds for both rewind and
  // hist modes). The parent maps these onto rewindOffsetSec or histAt.
  scrubMin: number;
  scrubMax: number;
  scrubValue: number;
  onScrubChange: (next: number) => void;
  // Seek button deltas, in seconds. Convention is +sec = "go back in time"
  // for rewind mode and -sec = "advance time" for both modes.
  onSeek: (deltaSec: number) => void;
  // "Now" button: jumps to live edge and returns to Live mode.
  onJumpNow: () => void;
  // Label shown left of the play button (e.g. "2h 5m ago" or "2026-05-18 14:30").
  label: string;
  // Playback state.
  playing: boolean;
  onPlayToggle: () => void;
  speed: PlaybackSpeed;
  onSpeedChange: (next: PlaybackSpeed) => void;
}

// Row 2 of the map command bar: seek + scrub + play/pause + speed.
// Lifted out of pages/Map.tsx unchanged in behavior; the React-Compiler slider
// workaround travels with it so the thumb keeps advancing during playback.
export function MapRewindControls({
  scrubMin,
  scrubMax,
  scrubValue,
  onScrubChange,
  onSeek,
  onJumpNow,
  label,
  playing,
  onPlayToggle,
  speed,
  onSpeedChange,
}: Props) {
  // <input type="range"> + React + React-Compiler quirk: once the input is
  // "dirty" from user interaction, React's controlled-input commit no longer
  // reliably touches the underlying DOM `.value` property, only the `value`
  // attribute. The thumb position (and Playwright's `input_value()`) reads
  // the property, so during playback the slider visually freezes even though
  // the state advances correctly. The fix uses the prototype setter directly
  // (sidesteps React's tracked setter) to push the value onto the DOM node
  // after every state change.
  const sliderRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    const el = sliderRef.current;
    if (el && el.value !== String(scrubValue)) {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        'value',
      )?.set;
      if (setter) setter.call(el, String(scrubValue));
      else el.value = String(scrubValue);
    }
  }, [scrubValue]);

  return (
    <div className="flex flex-wrap items-center gap-2" data-testid="map-rewind-controls">
      <span className="tabnum min-w-[5ch] whitespace-nowrap text-xs text-[var(--color-text-dim)]">
        {label}
      </span>
      <button
        type="button"
        onClick={onPlayToggle}
        aria-label={playing ? 'Pause playback' : 'Play playback'}
        data-testid="map-play-toggle"
        className={cn(
          'flex h-9 w-9 items-center justify-center rounded-full transition-colors',
          playing
            ? 'bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)]'
            : 'border border-[var(--color-border-default)] hover:bg-[var(--color-surface-2)]',
        )}
      >
        {playing ? <PauseIcon /> : <PlayIcon />}
      </button>
      <input
        ref={sliderRef}
        type="range"
        min={scrubMin}
        max={scrubMax}
        step={1}
        value={scrubValue}
        onChange={(e) => onScrubChange(Number(e.target.value))}
        className="map-rewind-range min-w-[120px] flex-1"
        aria-label="Scrubber"
        data-testid="map-rewind-slider"
      />
      <div className="flex items-center gap-1">
        <JumpButton label="−1h" testid="map-jump-back-1h" onClick={() => onSeek(+3600)} />
        <JumpButton label="−10m" testid="map-jump-back-10m" onClick={() => onSeek(+600)} />
        <JumpButton label="+10m" testid="map-jump-fwd-10m" onClick={() => onSeek(-600)} />
        <JumpButton label="+1h" testid="map-jump-fwd-1h" onClick={() => onSeek(-3600)} />
        <button
          type="button"
          onClick={onJumpNow}
          className="rounded-full bg-[var(--color-accent)] px-3 py-1 text-xs text-white hover:bg-[var(--color-accent-hover)]"
          data-testid="map-jump-now"
        >
          Now
        </button>
      </div>
      <div
        className="flex items-center gap-0.5 rounded-full border border-[var(--color-border-default)] p-0.5"
        role="group"
        aria-label="Playback speed"
        data-testid="map-speed-group"
      >
        {PLAYBACK_SPEEDS.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onSpeedChange(s)}
            aria-pressed={speed === s}
            data-testid={`map-speed-${s}x`}
            className={cn(
              'tabnum rounded-full px-2 py-0.5 text-xs',
              speed === s
                ? 'bg-[var(--color-accent)] text-white'
                : 'text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
            )}
          >
            {s}×
          </button>
        ))}
      </div>
    </div>
  );
}

function JumpButton({
  label,
  onClick,
  testid,
}: {
  label: string;
  onClick: () => void;
  testid: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testid}
      className="rounded-full border border-[var(--color-border-default)] px-2 py-1 text-xs hover:bg-[var(--color-surface-2)]"
    >
      {label}
    </button>
  );
}
