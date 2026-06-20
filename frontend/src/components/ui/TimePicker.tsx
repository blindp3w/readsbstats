import { useEffect, useRef, useState } from 'react';
import { ClockIcon } from '@radix-ui/react-icons';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover';
import { cn } from '@/lib/cn';

// Themed time picker replacing native `<input type="time">`. The native
// dropdown can't be styled and renders as a bright white widget over our
// dark theme. This wraps two scrollable HH / MM columns in our Radix
// Popover so the picker matches DatePicker visually.
//
// Round-trips 24h strings shaped `"HH:MM"` so callers keep the same shape
// they used with `<input type="time">`. Minute granularity defaults to 5
// to keep the column compact; pass `minuteStep={1}` for finer control.

interface TimePickerProps {
  id?: string;
  value: string; // 'HH:MM' or ''
  onChange: (next: string) => void;
  placeholder?: string;
  className?: string;
  ariaLabel?: string;
  minuteStep?: number;
  'data-testid'?: string;
  // Anchor side for the popover. Default 'bottom'. The map's bottom-fixed
  // command bar passes 'top' so the columns open upward.
  popoverSide?: 'top' | 'bottom' | 'left' | 'right';
}

function pad2(n: number): string {
  return n.toString().padStart(2, '0');
}

function parseTime(v: string): { h: number; m: number } | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(v);
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) return null;
  return { h, m: min };
}

export function TimePicker({
  id,
  value,
  onChange,
  placeholder = '--:--',
  className,
  ariaLabel,
  minuteStep = 5,
  'data-testid': testid,
  popoverSide = 'bottom',
}: TimePickerProps) {
  const [open, setOpen] = useState(false);
  const parsed = parseTime(value);

  // Track the in-popover pending selection so the user can click hour first,
  // then minute (or vice versa), then have the popover close. We require
  // BOTH columns be touched in the same popover session before committing;
  // otherwise a click on the hour column with an inherited minute would
  // close the popover before the user reaches the minute column.
  const [pendingH, setPendingH] = useState<number | null>(parsed?.h ?? null);
  const [pendingM, setPendingM] = useState<number | null>(parsed?.m ?? null);
  const [touchedH, setTouchedH] = useState(false);
  const [touchedM, setTouchedM] = useState(false);

  // Commit + close once the user has touched both columns in this session.
  // Inlined into the pick handlers (rather than driven by a useEffect on
  // pendingH/pendingM) so the close action doesn't cascade through a
  // render — react-hooks/set-state-in-effect.
  const tryCommit = (h: number | null, m: number | null) => {
    if (h == null || m == null) return;
    const next = `${pad2(h)}:${pad2(m)}`;
    if (next !== value) onChange(next);
    setOpen(false);
  };

  const pickH = (n: number) => {
    setPendingH(n);
    setTouchedH(true);
    if (touchedM) tryCommit(n, pendingM);
  };
  const pickM = (n: number) => {
    setPendingM(n);
    setTouchedM(true);
    if (touchedH) tryCommit(pendingH, n);
  };

  const handleOpenChange = (next: boolean) => {
    // On open, reset the pending/touched state so a fresh interaction
    // starts cleanly. Doing the reset here (event handler) rather than
    // in a useEffect avoids cascading-render warnings.
    if (next) {
      setPendingH(parsed?.h ?? null);
      setPendingM(parsed?.m ?? null);
      setTouchedH(false);
      setTouchedM(false);
    }
    setOpen(next);
  };

  const hours = Array.from({ length: 24 }, (_, i) => i);
  const minutes: number[] = [];
  for (let m = 0; m < 60; m += minuteStep) minutes.push(m);
  // Include the current value's minute even when it's off the step grid (e.g. a
  // URL-supplied 09:37 with minuteStep=5), so it shows and highlights instead of
  // vanishing — otherwise the inherited minute is invisible. audit 2026-06-15.
  if (parsed && !minutes.includes(parsed.m)) {
    minutes.push(parsed.m);
    minutes.sort((a, b) => a - b);
  }

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <button
          id={id}
          type="button"
          aria-label={ariaLabel ?? placeholder}
          data-testid={testid}
          data-empty={!parsed}
          className={cn(
            'flex w-full items-center justify-between gap-2 rounded border px-3 py-2 text-sm',
            'border-[var(--color-border-default)] bg-[var(--color-bg)]',
            'focus:border-[var(--color-accent)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]',
            'hover:bg-[var(--color-surface-2)]/40',
            parsed ? 'text-[var(--color-text)]' : 'text-[var(--color-text-dim)]',
            className,
          )}
        >
          <span className="truncate tabnum">{parsed ? value : placeholder}</span>
          <ClockIcon aria-hidden="true" className="shrink-0 text-[var(--color-text-dim)]" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-auto p-2"
        align="start"
        side={popoverSide}
        data-testid="time-picker-popover"
      >
        <div className="flex gap-1">
          <ScrollColumn
            label="Hour"
            values={hours}
            selected={pendingH ?? parsed?.h ?? null}
            onPick={pickH}
            testidPrefix="tp-h"
          />
          <ScrollColumn
            label="Min"
            values={minutes}
            selected={pendingM ?? parsed?.m ?? null}
            onPick={pickM}
            testidPrefix="tp-m"
          />
        </div>
      </PopoverContent>
    </Popover>
  );
}

function ScrollColumn({
  label,
  values,
  selected,
  onPick,
  testidPrefix,
}: {
  label: string;
  values: number[];
  selected: number | null;
  onPick: (n: number) => void;
  testidPrefix: string;
}) {
  // Scroll the selected row into view once, when the column first mounts (Radix
  // unmounts PopoverContent on close, so this remounts on each open). Guard with
  // a ref so a later pick — which changes `selected` — doesn't re-center the
  // column mid-interaction, yanking the user's scroll position (Audit 2026-06-20).
  const ref = useRef<HTMLUListElement>(null);
  const didScroll = useRef(false);
  useEffect(() => {
    if (didScroll.current) return;
    didScroll.current = true; // mark first-run before the null check (at-most-once)
    if (selected == null || !ref.current) return;
    const el = ref.current.querySelector<HTMLElement>(`[data-value="${selected}"]`);
    el?.scrollIntoView({ block: 'center' });
  }, [selected]);

  return (
    <div className="flex flex-col">
      <div className="px-2 pb-1 text-center text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
        {label}
      </div>
      <ul
        ref={ref}
        className="h-[180px] w-[64px] overflow-y-auto rounded border border-[var(--color-border-default)] bg-[var(--color-surface)] py-1 [scrollbar-width:thin]"
        role="listbox"
      >
        {values.map((n) => {
          const isActive = n === selected;
          return (
            <li key={n}>
              <button
                type="button"
                role="option"
                aria-selected={isActive}
                data-value={n}
                data-testid={`${testidPrefix}-${pad2(n)}`}
                onClick={() => onPick(n)}
                className={cn(
                  'tabnum block w-full px-3 py-1 text-center text-sm transition-colors',
                  isActive
                    ? 'bg-[var(--color-accent)] text-white hover:bg-[var(--color-accent-hover)]'
                    : 'text-[var(--color-text)] hover:bg-[var(--color-surface-2)]',
                )}
              >
                {pad2(n)}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
