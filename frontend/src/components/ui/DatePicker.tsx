import { useState } from 'react';
import { CalendarIcon, ChevronLeftIcon, ChevronRightIcon } from '@radix-ui/react-icons';
import { DayPicker, type Matcher } from 'react-day-picker';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover';
import { cn } from '@/lib/cn';
import { parseYMD } from '@/lib/dateParse';

// Themed date picker replacing native `<input type="date">`. The native popup
// can't be styled (only a `-webkit-calendar-picker-indicator` pseudo-element);
// this wraps `react-day-picker` inside our Radix Popover so the calendar
// matches the dark theme.
//
// Round-trips ISO date strings (`YYYY-MM-DD`) so callers can keep using the
// same URL-state shape as `<input type="date">`. Use this for date-only fields
// (History From/To); RangePicker's CustomRangeForm pairs this with a `<input
// type="time">` for the time portion.

interface DatePickerProps {
  id?: string;
  value: string; // 'YYYY-MM-DD' or ''
  onChange: (next: string) => void;
  placeholder?: string;
  className?: string;
  // Some callers want the trigger button to behave more like an Input
  // (smaller padding, same height). Default keeps the Input shape so dropping
  // it into a Field works without layout shifts.
  ariaLabel?: string;
  'data-testid'?: string;
  // Forwarded to DayPicker's `disabled` prop. Lets callers restrict
  // selectable dates (e.g. the map's HIST mode caps to map_history_hours).
  disabledMatcher?: Matcher | Matcher[];
  // Opens the popover on first mount. Used by the map's HIST mode so
  // switching to HIST surfaces the date picker without an extra tap.
  defaultOpen?: boolean;
  // Anchor side for the popover. Default 'bottom'. The map's bottom-fixed
  // command bar passes 'top' so the calendar opens upward.
  popoverSide?: 'top' | 'bottom' | 'left' | 'right';
}

function parseISO(v: string): Date | undefined {
  if (!v) return undefined;
  // Anchor parsing at local midnight so the displayed date doesn't shift
  // backwards in negative-offset timezones (UTC parse + local display).
  // parseYMD round-trip-rejects impossible dates (2026-02-31) rather than
  // rolling them over to the next month.
  const p = parseYMD(v);
  return p ? new Date(p.y, p.mo, p.d) : undefined;
}

function toISO(d: Date): string {
  const y = d.getFullYear();
  const m = (d.getMonth() + 1).toString().padStart(2, '0');
  const day = d.getDate().toString().padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function formatDisplay(d: Date | undefined, placeholder: string): string {
  if (!d) return placeholder;
  return d.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

export function DatePicker({
  id,
  value,
  onChange,
  placeholder = 'dd/mm/yyyy',
  className,
  ariaLabel,
  'data-testid': testid,
  disabledMatcher,
  defaultOpen = false,
  popoverSide = 'bottom',
}: DatePickerProps) {
  const [open, setOpen] = useState(defaultOpen);
  // Follow defaultOpen changes after mount (the map's HIST mode flips it on a
  // persistent instance) without a set-state-in-effect (lint-forbidden): adjust
  // during render via the previous-prop pattern. Manual open/close via
  // onOpenChange still works — this only fires when the prop itself changes.
  // audit 2026-06-15.
  const [prevDefaultOpen, setPrevDefaultOpen] = useState(defaultOpen);
  if (defaultOpen !== prevDefaultOpen) {
    setPrevDefaultOpen(defaultOpen);
    setOpen(defaultOpen);
  }
  const selected = parseISO(value);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          id={id}
          type="button"
          aria-label={ariaLabel ?? placeholder}
          data-testid={testid}
          data-empty={!selected}
          className={cn(
            'flex w-full items-center justify-between gap-2 rounded border px-3 py-2 text-sm',
            'border-[var(--color-border-default)] bg-[var(--color-bg)]',
            'focus:border-[var(--color-accent)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]',
            'hover:bg-[var(--color-surface-2)]/40',
            selected ? 'text-[var(--color-text)]' : 'text-[var(--color-text-dim)]',
            className,
          )}
        >
          <span className="truncate tabnum">{formatDisplay(selected, placeholder)}</span>
          <CalendarIcon aria-hidden="true" className="shrink-0 text-[var(--color-text-dim)]" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-auto p-2"
        align="start"
        side={popoverSide}
        data-testid="date-picker-popover"
      >
        <DayPicker
          mode="single"
          selected={selected}
          defaultMonth={selected}
          onSelect={(d) => {
            if (d) {
              onChange(toISO(d));
              setOpen(false);
            }
          }}
          disabled={disabledMatcher}
          showOutsideDays
          weekStartsOn={1}
          components={{
            Chevron: ({ orientation }) =>
              orientation === 'left' ? (
                <ChevronLeftIcon aria-hidden="true" />
              ) : (
                <ChevronRightIcon aria-hidden="true" />
              ),
          }}
          classNames={{
            root: 'rdp-root text-sm text-[var(--color-text)]',
            months: 'flex flex-col gap-2',
            month: 'space-y-2',
            month_caption: 'flex items-center justify-center px-2 py-1 text-sm font-medium',
            caption_label: 'text-[var(--color-text)]',
            nav: 'absolute inset-x-2 top-1 flex items-center justify-between pointer-events-none',
            button_previous:
              'pointer-events-auto inline-flex h-7 w-7 items-center justify-center rounded text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]',
            button_next:
              'pointer-events-auto inline-flex h-7 w-7 items-center justify-center rounded text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]',
            month_grid: 'border-collapse',
            weekdays: 'flex',
            weekday:
              'w-9 text-center text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-dim)]',
            weeks: '',
            week: 'flex w-full',
            day: 'h-9 w-9 p-0 text-center',
            day_button:
              'inline-flex h-9 w-9 items-center justify-center rounded tabnum text-[var(--color-text)] hover:bg-[var(--color-surface-2)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)] disabled:opacity-40 disabled:cursor-not-allowed',
            selected:
              '[&_button]:!bg-[var(--color-accent)] [&_button]:!text-white [&_button]:hover:!bg-[var(--color-accent-hover)]',
            today: '[&_button]:font-semibold [&_button]:underline [&_button]:underline-offset-4',
            outside: '[&_button]:text-[var(--color-text-dim)] [&_button]:opacity-50',
            disabled: '[&_button]:opacity-40 [&_button]:cursor-not-allowed',
            hidden: 'invisible',
          }}
        />
      </PopoverContent>
    </Popover>
  );
}
