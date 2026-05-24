// Single label / value / optional-sublabel tile for the Flight detail
// compact header's 4×2 metric grid (M3.1). Lighter than KpiCard — no
// Card chrome, no sparkline, no tooltip, no delta.
//
// Layout is purely vertical: label (small uppercase dim) → value
// (tabnum, medium) → optional sublabel (tabnum, dim).

import { type ReactNode } from 'react';

interface Props {
  label: string;
  value: ReactNode;
  sublabel?: ReactNode;
  testid?: string;
}

export function MetricCell({ label, value, sublabel, testid }: Props) {
  return (
    <div
      data-testid={testid}
      aria-label={
        typeof sublabel === 'string' && sublabel
          ? `${label} ${typeof value === 'string' ? value : ''} — ${sublabel}`
          : `${label} ${typeof value === 'string' ? value : ''}`
      }
    >
      <div className="text-[10px] font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
        {label}
      </div>
      <div className="tabnum text-sm font-medium leading-tight">{value}</div>
      {sublabel ? (
        <div className="tabnum mt-0.5 text-[10px] text-[var(--color-text-dim)]">{sublabel}</div>
      ) : null}
    </div>
  );
}
