import { useState, type ReactNode } from 'react';
import { ChevronDownIcon, ChevronUpIcon } from '@radix-ui/react-icons';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
import { Button } from '@/components/ui/Button';
import { Label } from '@/components/ui/Label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover';
import { DatePicker } from '@/components/ui/DatePicker';
import { TimePicker } from '@/components/ui/TimePicker';
import { cn } from '@/lib/cn';
import type { RangeState, RangeValue } from './useRange';

// Range picker UI. The `useRange` hook + RangeValue / RangeState types
// live in ./useRange so this file only exports the component itself
// (react-refresh/only-export-components hygiene). Consumers import
// `useRange` from '@/components/useRange' directly. Type-only
// re-exports below cost nothing at runtime and keep the
// `import type { RangeValue }` ergonomics co-located with RangePicker.

export type { RangeValue, RangeState } from './useRange';

const PRESETS: { value: RangeValue; label: string }[] = [
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: '90d', label: '90d' },
  { value: 'all', label: 'All' },
];

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
          {/* Key on the (from, to) pair forces a remount whenever the
              parent's range changes externally (preset click while the
              popover is open). The form's `useState` initialisers then
              re-run, so we don't need a reset effect — see CustomRangeForm. */}
          <CustomRangeForm
            key={`${state.from ?? 'n'}-${state.to ?? 'n'}`}
            initialFrom={state.from ?? null}
            initialTo={state.to ?? null}
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
  initialFrom: number | null;
  initialTo: number | null;
  onApply: (from: number, to: number) => void;
}) {
  // Defaults computed inside the useState initialiser so `Date.now()`
  // runs once per mount, not during render. Combined with the parent's
  // `key={…}` prop, external range changes trigger a remount and the
  // initialisers run with the new values.
  const [fromDate, setFromDate] = useState(() =>
    epochToLocalDate(initialFrom ?? Math.floor(Date.now() / 1000) - 86400),
  );
  const [fromTime, setFromTime] = useState(() =>
    epochToLocalTime(initialFrom ?? Math.floor(Date.now() / 1000) - 86400),
  );
  const [toDate, setToDate] = useState(() =>
    epochToLocalDate(initialTo ?? Math.floor(Date.now() / 1000)),
  );
  const [toTime, setToTime] = useState(() =>
    epochToLocalTime(initialTo ?? Math.floor(Date.now() / 1000)),
  );
  const [error, setError] = useState<string | null>(null);

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
