// Big-number KPI card. Label + value + optional delta line + optional
// sparkline. Tone/icon logic mirrors the previous Stats.tsx::TrendCard.

import { type ReactNode } from 'react';
import { TriangleUpIcon, TriangleDownIcon, DotFilledIcon } from '@radix-ui/react-icons';
import { Card, CardContent } from '@/components/ui/Card';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { KpiSparkline } from './KpiSparkline';

interface Props {
  label: string;
  value: number | string;
  prev?: number | null;
  series?: number[];
  sublabel?: ReactNode;
  testid?: string;
}

function classifyDelta(value: number | string, prev: number | null | undefined) {
  if (prev == null || typeof value !== 'number') return null;
  const delta = value - prev;
  const pct = prev > 0 ? (delta / prev) * 100 : null;
  return { delta, pct };
}

export function KpiCard({ label, value, prev, series, sublabel, testid }: Props) {
  const cmp = classifyDelta(value, prev);
  const ArrowIcon =
    cmp == null
      ? null
      : cmp.delta > 0
        ? TriangleUpIcon
        : cmp.delta < 0
          ? TriangleDownIcon
          : DotFilledIcon;
  const tone =
    cmp == null || cmp.delta === 0
      ? 'text-[var(--color-text-dim)]'
      : cmp.delta > 0
        ? 'text-[var(--color-success)]'
        : 'text-[var(--color-danger)]';

  const deltaLine =
    cmp == null ? (
      <span className="text-[var(--color-text-dim)]">—</span>
    ) : (
      <span className={`inline-flex items-center gap-1 ${tone}`}>
        {ArrowIcon ? <ArrowIcon aria-hidden="true" /> : null}
        <span>
          {cmp.delta >= 0 ? '+' : '−'}
          {Math.abs(cmp.delta).toLocaleString()}
          {cmp.pct != null ? ` (${cmp.pct >= 0 ? '+' : ''}${cmp.pct.toFixed(0)}%)` : ''}
        </span>
      </span>
    );

  const tooltipContent =
    cmp == null ? (
      'No previous period data'
    ) : (
      <span className="inline-flex items-center gap-1">
        {ArrowIcon ? <ArrowIcon aria-hidden="true" /> : null}
        <span className={tone}>
          {cmp.delta >= 0 ? '+' : '−'}
          {Math.abs(cmp.delta).toLocaleString()}
          {cmp.pct != null ? ` (${cmp.pct >= 0 ? '+' : ''}${cmp.pct.toFixed(0)}%)` : ''}
        </span>
        <span className="text-[var(--color-text-dim)]">vs previous period</span>
      </span>
    );

  const valueText = typeof value === 'number' ? value.toLocaleString() : value;
  const ariaLabel =
    cmp == null
      ? `${label} ${valueText}`
      : `${label} ${valueText}, ${cmp.delta >= 0 ? 'up' : 'down'} ${Math.abs(cmp.delta).toLocaleString()}${
          cmp.pct != null ? ` (${Math.abs(cmp.pct).toFixed(0)} percent)` : ''
        } vs previous period`;

  const hasSparkline = !!series && series.length >= 7;

  return (
    <SimpleTooltip content={tooltipContent} delayDuration={300}>
      {/* h-full on the wrapper too so Card's h-full has a stretched parent
          to resolve against — defensive against future TooltipTrigger
          asChild-semantics changes that would otherwise let the wrapper
          collapse to content-height. */}
      <div className="h-full">
        <Card className="card-hover h-full" data-testid={testid} aria-label={ariaLabel}>
          <CardContent className="flex h-full flex-col gap-1 pt-4">
            <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              {label}
            </div>
            <div className="tabnum text-2xl font-bold leading-tight sm:text-3xl">{valueText}</div>
            <div className="text-xs tabnum min-h-[1rem]">{deltaLine}</div>
            {/* Sublabel slot: always reserved so cards line up across the
                row even when one card has nothing useful to put here. */}
            <div className="min-h-[1rem] text-xs text-[var(--color-text-dim)]">
              {sublabel ?? (
                <span className="inline-block h-px w-8 bg-[var(--color-border-default)]" />
              )}
            </div>
            {/* Sparkline slot: same trick. h-6 (24 px) matches KpiSparkline's
                default height; empty cards get a 1-px dim baseline. mt-auto
                pins it to the card's bottom regardless of value/sublabel
                line-wrap so every card in the row aligns to the same
                visual baseline. */}
            <div className="mt-auto flex h-6 items-center pt-1">
              {hasSparkline ? (
                <KpiSparkline data={series!} ariaLabel={`${label} trend`} />
              ) : (
                <span
                  aria-hidden="true"
                  className="block h-px w-full bg-[var(--color-border-default)]"
                />
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </SimpleTooltip>
  );
}
