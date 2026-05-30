// Receiver health header for the Metrics page.
// Replaces the previous inline HealthBanner with a denser layout that
// surfaces the health state at a glance:
//
//   ┌────────────────────────────────────────────────────────────────┐
//   │ ■ ■ ■ ■ ■ ■ ■ ■ ■    9 checks · 7 OK · 1 warn · 1 down  [▾]    │
//   └────────────────────────────────────────────────────────────────┘
//   ⚠ message_rate  1008/min vs 641/min baseline (157%)
//   ✕ signal_drop   -39.6 dBFS vs -41.2 dBFS baseline (Δ -1.6 dB)
//
//   [expanded panel — existing per-check list, shown when ▾ toggled]
//
// Implements M2.3 + M2.4 from internal_docs/uiux/CLAUDE_DESIGN_BRIEF.md.
// Data comes verbatim from /api/metrics/health; check.message is already
// human-readable so the inline summaries just render it.

import { useEffect, useRef, useState } from 'react';
import {
  CheckCircledIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  CrossCircledIcon,
  ExclamationTriangleIcon,
  InfoCircledIcon,
} from '@radix-ui/react-icons';
import { Alert } from '@/components/ui/Alert';
import { Card, CardContent } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { CHART_COLORS } from '@/components/charts/theme';
import { cn } from '@/lib/cn';

// Shape mirrors the existing HealthResp in Metrics.tsx so callers can pass
// the same TanStack-query result.
export interface HealthCheck {
  name: string;
  severity: 'ok' | 'warn' | 'critical' | 'info' | string;
  message?: string;
}

export interface HealthResp {
  overall: 'ok' | 'warn' | 'critical' | 'info' | string;
  as_of: number;
  checks: HealthCheck[];
}

interface Props {
  q: {
    data: HealthResp | undefined;
    isLoading: boolean;
    isError: boolean;
  };
}

// statusColor + StatusIcon were previously inline in Metrics.tsx; moved
// here so the stripe is self-contained. Not exported (drop `export`):
// no other file references it, and a non-component function export
// breaks `react-refresh/only-export-components`.
function statusColor(severity: string): string {
  if (severity === 'ok') return CHART_COLORS.success;
  if (severity === 'warn') return CHART_COLORS.warn;
  if (severity === 'critical') return CHART_COLORS.danger;
  return CHART_COLORS.textDim;
}

function StatusIcon({ status }: { status: string }) {
  const color = statusColor(status);
  const Icon =
    status === 'ok'
      ? CheckCircledIcon
      : status === 'warn'
        ? ExclamationTriangleIcon
        : status === 'critical'
          ? CrossCircledIcon
          : InfoCircledIcon;
  return <Icon width={16} height={16} style={{ color, flexShrink: 0 }} aria-hidden />;
}

function checkRowId(name: string): string {
  // Programmatic-focus target — the stripe square click handler focuses
  // this id after expanding the detail panel.
  return `health-check-${name}`;
}

export function HealthStripe({ q }: Props) {
  const [open, setOpen] = useState(false);
  // Name of the check whose detail row should receive focus once `open`
  // becomes true. Held in a ref (not state) so clearing it after focus
  // doesn't trigger a cascading render — react-hooks/set-state-in-effect.
  // The ref is only consulted on the open→true transition (effect below).
  // Click-while-already-open takes the synchronous path in
  // `openAndFocus` instead, since `setOpen(true)` is a no-op and would
  // not re-run this effect.
  const focusNameRef = useRef<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const name = focusNameRef.current;
    if (!name) return;
    const el = document.getElementById(checkRowId(name));
    if (el) el.focus();
    focusNameRef.current = null;
  }, [open]);

  if (q.isLoading) return <Skeleton className="h-16 w-full" data-testid="metrics-health-loading" />;
  if (q.isError || !q.data) {
    return (
      <Alert variant="warn" data-testid="health-unavailable">
        Receiver health checks unavailable — retry on next poll.
      </Alert>
    );
  }

  const data = q.data;
  const checks = data.checks;
  const counts = countBySeverity(checks);
  const failing = checks
    .filter((c) => c.severity === 'warn' || c.severity === 'critical')
    .slice(0, 2);

  const openAndFocus = (name: string) => {
    if (open) {
      // Panel already expanded — the target row is mounted, focus it
      // synchronously. Going via the ref + effect would not work here
      // because `setOpen(true)` bails out (state unchanged), the effect
      // wouldn't re-run, and the second-click target would never gain
      // focus.
      document.getElementById(checkRowId(name))?.focus();
      return;
    }
    focusNameRef.current = name;
    setOpen(true);
  };

  return (
    <Card data-testid="metrics-health-stripe">
      <CardContent className="space-y-2 py-3">
        {/* Row 1: stripe + summary + chevron. */}
        <div className="flex flex-wrap items-center gap-3">
          <div
            className={cn(
              'flex flex-1 items-center gap-1',
              // Soft min so the stripe stays wide enough to read at base.
              'min-w-[140px]',
            )}
            data-testid="health-stripe-squares"
          >
            {checks.length === 0 ? (
              <span className="text-xs text-[var(--color-text-dim)]">No checks</span>
            ) : (
              checks.map((c) => (
                <SimpleTooltip
                  key={c.name}
                  content={
                    <span>
                      <strong>{c.name}</strong>
                      {c.message ? ' · ' + c.message : ''}
                    </span>
                  }
                >
                  <button
                    type="button"
                    onClick={() => openAndFocus(c.name)}
                    data-testid="health-stripe-square"
                    data-severity={c.severity}
                    data-name={c.name}
                    aria-label={`${c.name} (${c.severity})`}
                    // Visible square has small height; padding around it
                    // gives a 44px touch target without inflating the
                    // visual chrome.
                    className="group flex h-11 min-w-[24px] flex-1 items-center justify-center rounded outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
                    style={{ maxWidth: 48 }}
                  >
                    <span
                      aria-hidden="true"
                      className="block h-6 w-full rounded-sm transition-opacity group-hover:opacity-80"
                      style={{ background: statusColor(c.severity) }}
                    />
                  </button>
                </SimpleTooltip>
              ))
            )}
          </div>

          <div
            className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--color-text-dim)]"
            data-testid="health-stripe-summary"
          >
            <span className="tabnum">
              {checks.length} check{checks.length === 1 ? '' : 's'}
            </span>
            {counts.ok > 0 && <span className="tabnum">· {counts.ok} OK</span>}
            {counts.warn > 0 && (
              <span className="tabnum text-[var(--color-warn)]">· {counts.warn} warn</span>
            )}
            {counts.critical > 0 && (
              <span className="tabnum text-[var(--color-danger)]">· {counts.critical} down</span>
            )}
            {counts.info > 0 && <span className="tabnum">· {counts.info} info</span>}
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              disabled={checks.length === 0}
              aria-expanded={open}
              aria-controls="metrics-health-detail"
              data-testid="metrics-health-toggle"
              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded text-[var(--color-text-dim)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)] disabled:cursor-not-allowed disabled:opacity-50"
            >
              {open ? <ChevronUpIcon aria-hidden /> : <ChevronDownIcon aria-hidden />}
              <span className="sr-only">
                {checks.length === 0 ? 'No checks available' : 'Toggle health detail'}
              </span>
            </button>
          </div>
        </div>

        {/* First-failing inline summaries (max 2). */}
        {failing.length > 0 && (
          <ul className="space-y-1 text-xs" data-testid="health-stripe-failing">
            {failing.map((c) => (
              <li key={c.name}>
                <button
                  type="button"
                  onClick={() => openAndFocus(c.name)}
                  data-testid={`health-stripe-failing-${c.name}`}
                  aria-label={`View ${c.severity} check ${c.name}`}
                  className="flex w-full items-start gap-2 rounded px-1 py-1 text-left hover:bg-[var(--color-surface-2)]/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
                >
                  <StatusIcon status={c.severity} />
                  <span className="font-mono" style={{ color: statusColor(c.severity) }}>
                    {c.name}
                  </span>
                  {c.message ? (
                    <span className="truncate text-[var(--color-text-dim)]">{c.message}</span>
                  ) : null}
                </button>
              </li>
            ))}
          </ul>
        )}

        {/* Expanded detail panel — kept structurally identical to the
            previous HealthBanner's <ul>, with the addition of id +
            tabIndex on each row so the stripe-square handler can focus
            the right one. */}
        {open && checks.length > 0 && (
          <ul
            id="metrics-health-detail"
            className="divide-y divide-[var(--color-border-default)] border-t border-[var(--color-border-default)]"
            data-testid="metrics-health-detail"
          >
            {checks.map((c) => (
              <li
                key={c.name}
                id={checkRowId(c.name)}
                tabIndex={-1}
                className="flex flex-wrap items-center gap-2 border-l-4 py-2 pl-3 pr-3 text-xs outline-none focus-visible:bg-[var(--color-surface-2)]/40"
                style={{ borderLeftColor: statusColor(c.severity) }}
                data-testid={`metrics-health-check-${c.name}`}
                data-status={c.severity}
              >
                <StatusIcon status={c.severity} />
                <span className="sr-only">{c.severity}:</span>
                <span className="font-medium">{c.name}</span>
                {c.message ? (
                  <span className="text-[var(--color-text-dim)]">{c.message}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function countBySeverity(checks: HealthCheck[]): {
  ok: number;
  warn: number;
  critical: number;
  info: number;
} {
  const c = { ok: 0, warn: 0, critical: 0, info: 0 };
  for (const ch of checks) {
    if (ch.severity === 'ok') c.ok++;
    else if (ch.severity === 'warn') c.warn++;
    else if (ch.severity === 'critical') c.critical++;
    else c.info++;
  }
  return c;
}
