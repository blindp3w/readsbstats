import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { EChartsOption } from 'echarts';
import type { StatsResponse } from '@/pages/Stats';
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
import { CHART_COLORS, baseOption } from '@/components/charts/theme';
import { EChart } from '@/components/charts/EChart';

type ViewKey = 'aircraft' | 'airlines' | 'countries' | 'visitors' | 'routes' | 'airports';

const VIEWS: { key: ViewKey; label: string }[] = [
  { key: 'aircraft', label: 'Aircraft types' },
  { key: 'airlines', label: 'Airlines' },
  { key: 'countries', label: 'Countries' },
  { key: 'visitors', label: 'Visitors' },
  { key: 'routes', label: 'Routes' },
  { key: 'airports', label: 'Airports' },
];

export interface Row {
  label: string;
  fullLabel: string;
  value: number;
  icao_hex?: string;
}

interface TopChartProps {
  loading: boolean;
  top_aircraft_types?: StatsResponse['top_aircraft_types'];
  top_airlines?: StatsResponse['top_airlines'];
  top_countries?: StatsResponse['top_countries'];
  frequent_aircraft?: StatsResponse['frequent_aircraft'];
  top_routes?: StatsResponse['top_routes'];
  top_airports?: StatsResponse['top_airports'];
}

const trunc = (s: string, n = 14) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

function buildRows(view: ViewKey, props: TopChartProps): Row[] {
  switch (view) {
    case 'aircraft':
      return (props.top_aircraft_types ?? []).slice(0, 15).map((r) => ({
        label: trunc(r.type || '—'),
        fullLabel: r.type + (r.type_desc ? ' — ' + r.type_desc : ''),
        value: r.flights,
      }));
    case 'airlines':
      return (props.top_airlines ?? []).slice(0, 15).map((r) => ({
        label: trunc(r.airline || '—'),
        fullLabel: r.airline_name ?? r.airline,
        value: r.flights,
      }));
    case 'countries':
      return (props.top_countries ?? []).slice(0, 15).map((r) => ({
        label: trunc(r.country || '—'),
        fullLabel: r.country,
        value: r.flights,
      }));
    case 'visitors':
      return (props.frequent_aircraft ?? []).slice(0, 15).map((r) => ({
        label: trunc(r.registration ?? r.icao_hex),
        fullLabel:
          (r.registration ?? r.icao_hex) +
          (r.aircraft_type ? ' (' + r.aircraft_type + ')' : ''),
        value: r.flights,
        icao_hex: r.icao_hex,
      }));
    case 'routes':
      return (props.top_routes ?? []).slice(0, 15).map((r) => ({
        label: (r.origin_icao || '???') + '→' + (r.dest_icao || '???'),
        fullLabel: (r.origin_icao || '???') + ' → ' + (r.dest_icao || '???'),
        value: r.flights,
      }));
    case 'airports':
      return (props.top_airports ?? []).slice(0, 15).map((r) => ({
        label: r.icao_code,
        fullLabel: r.icao_code + (r.name ? ' ' + r.name : ''),
        value: r.appearances ?? r.flights ?? 0,
      }));
  }
}

// Exported for unit tests.
export function buildTopChartOption(rows: Row[], clickable: boolean): EChartsOption {
  return {
    ...baseOption(),
    tooltip: {
      trigger: 'item',
      backgroundColor: CHART_COLORS.surface,
      borderColor: CHART_COLORS.grid,
      textStyle: { color: CHART_COLORS.text, fontSize: 12 },
      formatter: (p: any) => `${p.data.fullLabel} — ${p.data.value}`,
    },
    grid: { top: 8, right: 32, bottom: 8, left: 110, containLabel: false },
    xAxis: {
      type: 'value',
      axisLabel: { color: CHART_COLORS.textDim },
      splitLine: { lineStyle: { color: CHART_COLORS.grid, type: 'dashed' } },
    },
    yAxis: {
      type: 'category',
      data: rows.map((r) => r.label),
      axisLabel: {
        color: CHART_COLORS.textDim,
        formatter: (v: string) => trunc(v, 14),
        width: 100,
        overflow: 'truncate',
      },
      axisTick: { show: false },
      inverse: true,
    },
    series: [
      {
        type: 'bar',
        cursor: clickable ? 'pointer' : 'default',
        data: rows.map((r) => ({
          value: r.value,
          name: r.label,
          fullLabel: r.fullLabel,
          icao_hex: r.icao_hex,
        })),
        itemStyle: { color: CHART_COLORS.accent, borderRadius: [0, 3, 3, 0] },
      },
    ],
  };
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
        <Select
          value={view}
          onValueChange={(v) => setView(v as ViewKey)}
        >
          <div className="w-full sm:hidden">
            <SelectTrigger
              data-testid="stats-top-chart-select"
              aria-label="Statistics view"
            >
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
