import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
import { EChart } from '@/components/charts/EChart';
import { type RankingsData, type ViewKey, VIEWS, buildRows, buildTopChartOption } from './topRows';

// Re-exports kept for any out-of-tree imports (and the existing
// frontend/test/top-chart-click.test.tsx which imports the option builder).
export { buildTopChartOption } from './topRows';
export type { Row, ViewKey } from './topRows';

interface TopChartProps extends RankingsData {
  loading: boolean;
}

export function TopChart(props: TopChartProps) {
  const [view, setView] = useState<ViewKey>('aircraft');
  const navigate = useNavigate();

  const rows = buildRows(view, props);
  const clickable = view === 'visitors';
  const option = useMemo(() => buildTopChartOption(rows, clickable), [rows, clickable]);
  const onEvents = useMemo(
    () => ({
      click: (...args: unknown[]) => {
        if (!clickable) return;
        const p = args[0] as { data?: { icao_hex?: string } } | undefined;
        const hex = p?.data?.icao_hex;
        if (hex) navigate('/aircraft/' + hex);
      },
    }),
    [clickable, navigate],
  );

  return (
    <Card data-testid="stats-top-chart">
      <CardHeader className="flex flex-col items-start gap-2">
        <CardTitle>Top statistics</CardTitle>
        {/* < sm: 6 tabs don't fit iPhone portrait (~393 px); fall back to
            a dropdown. >= sm: ToggleGroup tabs, same as desktop. Both
            controls share the same `view` state. */}
        {/* Visibility lives on plain wrapper <div>s. Both SelectTrigger and
            ToggleGroupRoot set `inline-flex` in their base classes; our cn()
            is clsx (not tailwind-merge), so `sm:hidden` / `hidden sm:flex`
            applied directly lose the CSS-order battle to `inline-flex`. */}
        <Select value={view} onValueChange={(v) => setView(v as ViewKey)}>
          <div className="w-full sm:hidden">
            <SelectTrigger data-testid="stats-top-chart-select" aria-label="Statistics view">
              <SelectValue />
            </SelectTrigger>
          </div>
          <SelectContent>
            {VIEWS.map((v) => (
              <SelectItem key={v.key} value={v.key}>
                {v.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="hidden sm:block">
          <ToggleGroupRoot
            type="single"
            value={view}
            onValueChange={(v) => {
              if (!v) return;
              setView(v as ViewKey);
            }}
            aria-label="Statistics view"
          >
            {VIEWS.map((v) => (
              <ToggleGroupItem key={v.key} value={v.key}>
                {v.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroupRoot>
        </div>
      </CardHeader>
      <CardContent>
        {props.loading ? (
          <Skeleton className="h-[420px] w-full" />
        ) : rows.length === 0 ? (
          <div className="flex h-[420px] items-center justify-center">
            <p className="text-sm text-[var(--color-text-dim)]">No data.</p>
          </div>
        ) : (
          <EChart option={option} height={420} onEvents={onEvents} />
        )}
      </CardContent>
    </Card>
  );
}
