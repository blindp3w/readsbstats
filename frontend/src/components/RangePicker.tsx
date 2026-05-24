import { useEffect, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ChevronDownIcon, ChevronUpIcon } from '@radix-ui/react-icons';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
import { Button } from '@/components/ui/Button';
import { Label } from '@/components/ui/Label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover';
import { DatePicker } from '@/components/ui/DatePicker';
import { TimePicker } from '@/components/ui/TimePicker';
import { cn } from '@/lib/cn';

// Range picker — URL-state.
//
// Two storage shapes:
//   ?range=24h|7d|30d|90d|all   — relative-to-now window
//   ?from=<epoch>&to=<epoch>     — explicit absolute window (Custom)
//
// Custom takes precedence over `range`. When the user picks a preset, the
// from/to params are cleared from the URL so the page stays bookmarkable.
// The Custom UI floats in a Radix Popover anchored to the "Custom" button.

export type RangeValue = 'all' | '24h' | '7d' | '30d' | '90d' | 'custom';

const PRESETS: { value: RangeValue; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '90d', label: '90d' },
  { value: 'all', label: 'All' },
];

const PRESET_VALUES = new Set<RangeValue>(['24h', '7d', '30d', '90d', 'all']);

export interface RangeState {
  // Active preset (used to color the toggle). 'custom' is implied when from/to
  // are present without an explicit `range=custom`.
  value: RangeValue;
  // Resolved absolute window (always present for non-'all').
  from?: number;
  to?: number;
}

export function useRange(defaultValue: RangeValue = '24h'): {
  state: RangeState;
  setPreset: (v: RangeValue) => void;
  setCustom: (from: number, to: number) => void;
  clearCustom: () => void;
} {
  const [params, setParams] = useSearchParams();
  const fromRaw = params.get('from');
  const toRaw = params.get('to');
  const rangeRaw = params.get('range') as RangeValue | null;

  const hasCustom = fromRaw != null && toRaw != null;
  let value: RangeValue;
  if (hasCustom) value = 'custom';
  else if (rangeRaw && PRESET_VALUES.has(rangeRaw)) value = rangeRaw;
  else value = defaultValue;

  let from: number | undefined;
  let to: number | undefined;
  if (hasCustom) {
    from = Number(fromRaw);
    to = Number(toRaw);
    if (!Number.isFinite(from) || !Number.isFinite(to)) {
      from = undefined;
      to = undefined;
    }
  } else {
    const w = presetWindow(value);
    from = w.from;
    to = w.to;
  }

  const setPreset = (v: RangeValue) => {
    setParams((prev) => {
      const out = new URLSearchParams(prev);
      out.delete('from');
      out.delete('to');
      if (v === defaultValue) out.delete('range');
      else out.set('range', v);
      return out;
    });
  };

  const setCustom = (from_: number, to_: number) => {
    setParams((prev) => {
      const out = new URLSearchParams(prev);
      out.set('from', String(Math.floor(from_)));
      out.set('to', String(Math.floor(to_)));
      out.delete('range');
      return out;
    });
  };

  const clearCustom = () => setPreset(defaultValue);

  return { state: { value, from, to }, setPreset, setCustom, clearCustom };
}

function presetWindow(range: RangeValue): { from?: number; to?: number } {
  if (range === 'all' || range === 'custom') return {};
  const now = Math.floor(Date.now() / 1000);
  const sec =
    range === '24h'
      ? 86400
      : range === '7d'
        ? 7 * 86400
        : range === '30d'
          ? 30 * 86400
          : 90 * 86400;
  return { from: now - sec, to: now };
}

interface PickerProps {
  state: RangeState;
  onPreset: (v: RangeValue) => void;
  onCustom: (from: number, to: number) => void;
  options?: { value: RangeValue; label: string }[];
  // Show the "All" preset (Statistics page yes, Metrics page no).
  allowAll?: boolean;
  // When true, the picker wraps itself in a sticky container that docks
  // immediately under the top nav (offset = `var(--rsbs-nav-h)` from
  // index.css). Optional `right` slot renders inline at the far right —
  // intended for a refreshing indicator or secondary control. Default off
  // so existing call sites (Metrics, History, Map) are unaffected.
  sticky?: boolean;
  right?: ReactNode;
}

// Custom range stored as Unix epoch but edited as separate date + time fields
// in the local timezone (matches v1 + datetime-local <input> semantics).
function epochToLocalDate(epoch: number): string {
  const d = new Date(epoch * 1000);
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function epochToLocalTime(epoch: number): string {
  const d = new Date(epoch * 1000);
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function localDateTimeToEpoch(date: string, time: string): number | null {
  if (!date) return null;
  // Empty time defaults to 00:00; partial "HH" gets ":00" suffix.
  const t = time || '00:00';
  const iso = `${date}T${t.length === 5 ? t : `${t.padEnd(5, '0')}`}`;
  const ms = new Date(iso).getTime();
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

export function RangePicker({
  state,
  onPreset,
  onCustom,
  options = PRESETS,
  allowAll = true,
  sticky = false,
  right,
}: PickerProps) {
  const [open, setOpen] = useState(false);

  const visibleOptions = allowAll ? options : options.filter((o) => o.value !== 'all');
  const isCustom = state.value === 'custom';

  const inner = (
    <div className="flex flex-wrap items-center gap-2" data-testid="range-picker">
      <ToggleGroupRoot
        type="single"
        value={isCustom ? '' : state.value}
        onValueChange={(v) => {
          if (!v) return;
          onPreset(v as RangeValue);
        }}
        aria-label="Time range"
        className="flex-nowrap"
      >
        {visibleOptions.map((o) => (
          <ToggleGroupItem key={o.value} value={o.value} data-testid={`range-${o.value}`}>
            {o.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroupRoot>

      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-pressed={isCustom}
            className={cn(
              'inline-flex items-center gap-1 rounded border px-3 text-xs font-medium transition-colors',
              // Outer heights match the ToggleGroup tray (which adds its own
              // 2px padding around the 36/28px items → 40/32 total).
              'min-h-[40px] min-w-[44px] md:min-h-[32px]',
              isCustom
                ? 'border-[var(--color-accent)] bg-[var(--color-accent)] text-white'
                : 'border-[var(--color-border-default)] text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
            )}
            data-testid="range-custom-toggle"
          >
            <span>Custom</span>
            {open ? <ChevronUpIcon aria-hidden="true" /> : <ChevronDownIcon aria-hidden="true" />}
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-[320px]" data-testid="range-custom-panel">
          <CustomRangeForm
            initialFrom={state.from ?? Math.floor(Date.now() / 1000) - 86400}
            initialTo={state.to ?? Math.floor(Date.now() / 1000)}
            onApply={(from, to) => {
              onCustom(from, to);
              setOpen(false);
            }}
          />
        </PopoverContent>
      </Popover>
      {right ? <div className="ml-auto">{right}</div> : null}
    </div>
  );

  if (!sticky) return inner;

  return (
    <div
      // z-30 sits below the nav (z-[1000]) but above page content so the
      // bar doesn't get clipped on scroll. The negative inline margin +
      // matching padding re-creates the parent page's px-4 gutter so the
      // backdrop fills the full viewport width on stick.
      className="sticky z-30 -mx-4 border-b border-[var(--color-border-default)] bg-[var(--color-surface)]/85 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-[var(--color-surface)]/70"
      style={{ top: 'var(--rsbs-nav-h, 41px)' }}
      data-testid="range-picker-sticky"
    >
      {inner}
    </div>
  );
}

function CustomRangeForm({
  initialFrom,
  initialTo,
  onApply,
}: {
  initialFrom: number;
  initialTo: number;
  onApply: (from: number, to: number) => void;
}) {
  const [fromDate, setFromDate] = useState(() => epochToLocalDate(initialFrom));
  const [fromTime, setFromTime] = useState(() => epochToLocalTime(initialFrom));
  const [toDate, setToDate] = useState(() => epochToLocalDate(initialTo));
  const [toTime, setToTime] = useState(() => epochToLocalTime(initialTo));
  const [error, setError] = useState<string | null>(null);

  // Reset to current state if the popover re-opens after URL changed.
  useEffect(() => {
    setFromDate(epochToLocalDate(initialFrom));
    setFromTime(epochToLocalTime(initialFrom));
    setToDate(epochToLocalDate(initialTo));
    setToTime(epochToLocalTime(initialTo));
    setError(null);
  }, [initialFrom, initialTo]);

  const apply = () => {
    const a = localDateTimeToEpoch(fromDate, fromTime);
    const b = localDateTimeToEpoch(toDate, toTime);
    if (a == null || b == null) {
      setError('Both dates are required.');
      return;
    }
    if (a >= b) {
      setError('From must be earlier than To.');
      return;
    }
    onApply(a, b);
  };

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        apply();
      }}
    >
      <div>
        <Label htmlFor="range-custom-from">From</Label>
        <div className="flex gap-2">
          <DatePicker
            id="range-custom-from"
            value={fromDate}
            onChange={setFromDate}
            ariaLabel="From date"
            className="flex-1"
            data-testid="range-custom-from"
          />
          <TimePicker
            value={fromTime}
            onChange={setFromTime}
            ariaLabel="From time"
            className="w-[96px]"
            data-testid="range-custom-from-time"
          />
        </div>
      </div>
      <div>
        <Label htmlFor="range-custom-to">To</Label>
        <div className="flex gap-2">
          <DatePicker
            id="range-custom-to"
            value={toDate}
            onChange={setToDate}
            ariaLabel="To date"
            className="flex-1"
            data-testid="range-custom-to"
          />
          <TimePicker
            value={toTime}
            onChange={setToTime}
            ariaLabel="To time"
            className="w-[96px]"
            data-testid="range-custom-to-time"
          />
        </div>
      </div>
      {error && (
        <p className="text-xs text-[var(--color-danger)]" role="alert">
          {error}
        </p>
      )}
      <div className="flex justify-end">
        <Button type="submit" size="sm" data-testid="range-custom-apply">
          Apply
        </Button>
      </div>
    </form>
  );
}
