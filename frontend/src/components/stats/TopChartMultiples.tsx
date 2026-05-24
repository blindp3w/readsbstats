// xl-only small-multiples view of the six TopChart rankings. Renders 6
// compact horizontal-bar mini-charts in a 3-col grid (two rows). Reuses the
// shared option builder so colors/axes match the single-card variant.

import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { EChart } from '@/components/charts/EChart';
import {
  type RankingsData,
  type ViewKey,
  VIEWS,
  buildRows,
  buildTopChartOption,
} from '@/components/charts/topRows';

interface Props extends RankingsData {
  loading: boolean;
  topN?: number;
}

interface CellProps extends RankingsData {
  view: ViewKey;
  label: string;
  topN: number;
}

function MiniRankingCell({ view, label, topN, ...data }: CellProps) {
  const navigate = useNavigate();
  const rows = buildRows(view, data, topN);
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
    <Card data-testid={`stats-top-multiple-${view}`}>
      <CardHeader>
        <CardTitle className="text-sm">{label}</CardTitle>
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <div className="flex h-[200px] items-center justify-center">
            <p className="text-xs text-[var(--color-text-dim)]">No data.</p>
          </div>
        ) : (
          <EChart option={option} height={200} onEvents={onEvents} />
        )}
      </CardContent>
    </Card>
  );
}

export function TopChartMultiples(props: Props) {
  const { loading, topN = 8, ...data } = props;
  if (loading) {
    return (
      <div className="grid gap-4 xl:grid-cols-3" data-testid="stats-top-multiples">
        {VIEWS.map((v) => (
          <Skeleton key={v.key} className="h-[260px] w-full" />
        ))}
      </div>
    );
  }
  return (
    <div className="grid gap-4 xl:grid-cols-3" data-testid="stats-top-multiples">
      {VIEWS.map((v) => (
        <MiniRankingCell key={v.key} view={v.key} label={v.label} topN={topN} {...data} />
      ))}
    </div>
  );
}
