// Shared chart styling tokens — keep Recharts plots looking like the rest
// of the dark UI without per-chart prop boilerplate.

export const CHART_COLORS = {
  accent: '#5b9af9',
  success: '#22c55e',
  warn: '#eab308',
  orange: '#f97316',
  purple: '#a855f7',
  danger: '#ef4444',
  text: '#e6ebf5',
  textDim: '#8891aa',
  grid: '#2e3350',
  surface: '#161a26',
};

export const TOOLTIP_STYLE: React.CSSProperties = {
  background: CHART_COLORS.surface,
  border: `1px solid ${CHART_COLORS.grid}`,
  borderRadius: 6,
  fontSize: 12,
  padding: '6px 8px',
  color: CHART_COLORS.text,
};

export const TOOLTIP_LABEL_STYLE: React.CSSProperties = {
  color: CHART_COLORS.textDim,
  fontSize: 11,
};

export const AXIS_PROPS = {
  stroke: CHART_COLORS.textDim,
  fontSize: 11,
  tickLine: { stroke: CHART_COLORS.grid },
  axisLine: { stroke: CHART_COLORS.grid },
};
