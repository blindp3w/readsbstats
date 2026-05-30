// Polar range plot — port of v1's stats.js polar chart.
// Buckets are bearing buckets (e.g. 36 × 10°). Each value is the max
// distance recorded in that bucket. We render an SVG polar area.

import { useFormat } from '@/hooks/useFormat';
import { CHART_COLORS } from './theme';
import { polarToXY } from './chartMath';

interface Bucket {
  bearing: number; // degrees
  // /api/stats/polar returns `max_dist_nm`; older notes elsewhere use
  // `max_distance_nm`. We only consume the dist value, so accept either.
  max_dist_nm?: number;
  max_distance_nm?: number;
}

function bucketDist(b: Bucket): number {
  return b.max_dist_nm ?? b.max_distance_nm ?? 0;
}

interface Props {
  buckets: Bucket[] | undefined;
  size?: number;
}

const SIZE_DEFAULT = 320;

export function PolarRange({ buckets, size = SIZE_DEFAULT }: Props) {
  const { fmtDist } = useFormat();
  if (!buckets || buckets.length === 0) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-[var(--color-text-dim)]">
        no polar data yet
      </div>
    );
  }
  const max = Math.max(...buckets.map(bucketDist)) || 1;
  const cx = size / 2;
  const cy = size / 2;
  const radius = size / 2 - 24;

  // bearing→SVG coords lives in module-scoped `polarToXY` (above) so the
  // math is unit-testable. The closure inside the component just binds
  // the per-render geometry (cx, cy, radius, max) into a curried call.
  const toXY = (bearingDeg: number, distance: number): [number, number] =>
    polarToXY(bearingDeg, distance, max, cx, cy, radius);

  // Build polygon path through buckets (sorted by bearing).
  const sorted = [...buckets].sort((a, b) => a.bearing - b.bearing);
  const pathPoints = sorted.map((b) => toXY(b.bearing, bucketDist(b)));
  const polygon = pathPoints.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');

  // Concentric rings at 25/50/75/100 % of max
  const rings = [0.25, 0.5, 0.75, 1].map((f) => radius * f);
  // Cardinal directions
  const directions = [
    { label: 'N', angle: 0 },
    { label: 'E', angle: 90 },
    { label: 'S', angle: 180 },
    { label: 'W', angle: 270 },
  ];

  return (
    <div className="flex flex-col items-center gap-2" data-testid="polar-range">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        width="100%"
        style={{ maxWidth: size }}
        role="img"
        aria-label={`Polar range plot, max ${fmtDist(max)}`}
      >
        {/* Concentric rings */}
        {rings.map((r, i) => (
          <circle
            key={i}
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={CHART_COLORS.grid}
            strokeDasharray={i === rings.length - 1 ? undefined : '2 4'}
          />
        ))}
        {/* Cross hairs */}
        <line
          x1={cx}
          y1={cy - radius}
          x2={cx}
          y2={cy + radius}
          stroke={CHART_COLORS.grid}
          strokeDasharray="2 4"
        />
        <line
          x1={cx - radius}
          y1={cy}
          x2={cx + radius}
          y2={cy}
          stroke={CHART_COLORS.grid}
          strokeDasharray="2 4"
        />

        {/* Data polygon */}
        <polygon
          points={polygon}
          fill={`${CHART_COLORS.accent}40`}
          stroke={CHART_COLORS.accent}
          strokeWidth={1.5}
          strokeLinejoin="round"
        />

        {/* Bucket points (subtle) */}
        {pathPoints.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={1.5} fill={CHART_COLORS.accent} />
        ))}

        {/* Direction labels */}
        {directions.map((d) => {
          const a = ((d.angle - 90) * Math.PI) / 180;
          const lx = cx + (radius + 12) * Math.cos(a);
          const ly = cy + (radius + 12) * Math.sin(a);
          return (
            <text
              key={d.label}
              x={lx}
              y={ly}
              textAnchor="middle"
              dominantBaseline="middle"
              fontSize={11}
              fill={CHART_COLORS.textDim}
            >
              {d.label}
            </text>
          );
        })}
      </svg>
      <div className="text-xs text-[var(--color-text-dim)] tabnum">max {fmtDist(max)}</div>
    </div>
  );
}
