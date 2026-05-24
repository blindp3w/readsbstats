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

  return (
    <SimpleTooltip content={tooltipContent} delayDuration={300}>
      <div>
        <Card className="card-hover" data-testid={testid} aria-label={ariaLabel}>
          <CardContent className="space-y-1 pt-4">
            <div className="text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
              {label}
            </div>
            <div className="tabnum text-3xl font-bold leading-tight">{valueText}</div>
            <div className="text-xs tabnum min-h-[1rem]">{deltaLine}</div>
            {sublabel ? (
              <div className="text-xs text-[var(--color-text-dim)]">{sublabel}</div>
            ) : null}
            {series && series.length > 0 ? (
              <div className="pt-1">
                <KpiSparkline data={series} ariaLabel={`${label} trend`} />
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </SimpleTooltip>
  );
}
