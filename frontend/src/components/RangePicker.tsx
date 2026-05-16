import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
import { Input } from '@/components/ui/Input';
import { Button } from '@/components/ui/Button';
import { Label } from '@/components/ui/Label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover';
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
    range === '24h' ? 86400 : range === '7d' ? 7 * 86400 : range === '30d' ? 30 * 86400 : 90 * 86400;
  return { from: now - sec, to: now };
}

interface PickerProps {
  state: RangeState;
  onPreset: (v: RangeValue) => void;
  onCustom: (from: number, to: number) => void;
  options?: { value: RangeValue; label: string }[];
  // Show the "All" preset (Statistics page yes, Metrics page no).
  allowAll?: boolean;
}

// datetime-local <input> uses local-time strings shaped "YYYY-MM-DDTHH:mm".
// Conversion to/from Unix epoch goes through the user's local TZ — matches
// v1's behaviour.
function epochToLocalInput(epoch: number): string {
  const d = new Date(epoch * 1000);
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function localInputToEpoch(s: string): number | null {
  if (!s) return null;
  const t = new Date(s).getTime();
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

export function RangePicker({
  state,
  onPreset,
  onCustom,
  options = PRESETS,
  allowAll = true,
}: PickerProps) {
  const [open, setOpen] = useState(false);

  const visibleOptions = allowAll ? options : options.filter((o) => o.value !== 'all');
  const isCustom = state.value === 'custom';

  return (
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
            Custom {open ? '▴' : '▾'}
          </button>
        </PopoverTrigger>
        <PopoverContent className="w-[260px]" data-testid="range-custom-panel">
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
  const [from, setFrom] = useState(() => epochToLocalInput(initialFrom));
  const [to, setTo] = useState(() => epochToLocalInput(initialTo));
  const [error, setError] = useState<string | null>(null);

  // Reset to current state if the popover re-opens after URL changed.
  useEffect(() => {
    setFrom(epochToLocalInput(initialFrom));
    setTo(epochToLocalInput(initialTo));
    setError(null);
  }, [initialFrom, initialTo]);

  const apply = () => {
    const a = localInputToEpoch(from);
    const b = localInputToEpoch(to);
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
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        apply();
      }}
    >
      <div>
        <Label htmlFor="range-custom-from">From</Label>
        <Input
          id="range-custom-from"
          type="datetime-local"
          value={from}
          onChange={(e) => setFrom(e.target.value)}
          className="text-xs"
          data-testid="range-custom-from"
        />
      </div>
      <div>
        <Label htmlFor="range-custom-to">To</Label>
        <Input
          id="range-custom-to"
          type="datetime-local"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          className="text-xs"
          data-testid="range-custom-to"
        />
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
