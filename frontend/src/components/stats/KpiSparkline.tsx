// Tiny inline SVG sparkline. No ECharts dep — keeps the lazy `charts`
// chunk untouched for an otherwise-static <120-point path. Mirrors the
// SVG approach in PolarRange.tsx. y-axis is relative (min/max of series),
// not zero-baselined — see Tufte. Below MIN_POINTS the shape is noise
// rather than signal, so we render nothing.

import { CHART_COLORS } from '@/components/charts/theme';

const MIN_POINTS = 7;

interface Props {
  data: number[];
  width?: number;
  height?: number;
  ariaLabel?: string;
}

export function KpiSparkline({ data, width = 120, height = 24, ariaLabel }: Props) {
  if (data.length < MIN_POINTS) return null;

  let min = Infinity;
  let max = -Infinity;
  for (const v of data) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  // Flat series: render a single horizontal line through the middle.
  const range = max - min || 1;

  const stepX = width / (data.length - 1);
  // Inset y by 1 px so the stroke doesn't get clipped by the viewBox.
  const yPad = 1;
  const innerH = height - yPad * 2;
  const points = data
    .map((v, i) => {
      const x = i * stepX;
      const y = yPad + innerH - ((v - min) / range) * innerH;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel ?? `Trend over ${data.length} points`}
      data-testid="kpi-sparkline"
    >
      <polyline
        points={points}
        fill="none"
        stroke={CHART_COLORS.accent}
        strokeWidth={1.25}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
