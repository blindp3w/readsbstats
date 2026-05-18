import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';
import type { StatsResponse } from '@/pages/Stats';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
import {
  AXIS_PROPS,
  CHART_COLORS,
  TOOLTIP_LABEL_STYLE,
  TOOLTIP_STYLE,
} from '@/components/charts/theme';

type ViewKey = 'aircraft' | 'airlines' | 'countries' | 'visitors' | 'routes' | 'airports';

const VIEWS: { key: ViewKey; label: string }[] = [
  { key: 'aircraft', label: 'Aircraft types' },
  { key: 'airlines', label: 'Airlines' },
  { key: 'countries', label: 'Countries' },
  { key: 'visitors', label: 'Visitors' },
  { key: 'routes', label: 'Routes' },
  { key: 'airports', label: 'Airports' },
];

interface Row {
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

export function TopChart(props: TopChartProps) {
  const [view, setView] = useState<ViewKey>('aircraft');
  const navigate = useNavigate();

  const rows = buildRows(view, props);
  const labelMap = Object.fromEntries(rows.map((r) => [r.label, r.fullLabel]));

  return (
    <Card data-testid="stats-top-chart">
      <CardHeader className="flex flex-col items-start gap-2">
        <CardTitle>Top statistics</CardTitle>
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
      </CardHeader>
      <CardContent>
        {props.loading ? (
          <Skeleton className="h-[420px] w-full" />
        ) : rows.length === 0 ? (
          <div className="flex h-[420px] items-center justify-center">
            <p className="text-sm text-[var(--color-text-dim)]">No data.</p>
          </div>
        ) : (
          <div style={{ width: '100%', height: 420 }}>
            <ResponsiveContainer>
              <BarChart
                layout="vertical"
                data={rows}
                margin={{ top: 4, right: 32, left: 0, bottom: 4 }}
              >
                <CartesianGrid
                  stroke={CHART_COLORS.grid}
                  strokeDasharray="2 4"
                  horizontal={false}
                />
                <XAxis type="number" allowDecimals={false} {...AXIS_PROPS} />
                <YAxis
                  type="category"
                  dataKey="label"
                  width={100}
                  {...AXIS_PROPS}
                  tickLine={false}
                />
                <Tooltip
                  cursor={{ fill: CHART_COLORS.surface }}
                  contentStyle={TOOLTIP_STYLE}
                  labelStyle={TOOLTIP_LABEL_STYLE}
                  labelFormatter={(label) => labelMap[String(label)] ?? String(label)}
                />
                <Bar
                  dataKey="value"
                  fill={CHART_COLORS.accent}
                  radius={[0, 3, 3, 0]}
                  cursor={view === 'visitors' ? 'pointer' : 'default'}
                  onClick={
                    view === 'visitors'
                      ? (data: any) => {
                          if (data?.icao_hex) navigate('/aircraft/' + data.icao_hex);
                        }
                      : undefined
                  }
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
