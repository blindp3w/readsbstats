import { useEffect, useRef } from 'react';
import type { EChartsOption } from 'echarts';
import { echarts } from './echarts-setup';

type ECharts = ReturnType<typeof echarts.init>;

interface Props {
  option: EChartsOption;
  height?: number | string;
  group?: string;
  onEvents?: Record<string, (...args: unknown[]) => void>;
}

export function EChart({ option, height = 220, group, onEvents }: Props) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<ECharts | null>(null);

  // Mount + dispose.
  useEffect(() => {
    if (!hostRef.current) return;
    const chart = echarts.init(hostRef.current, undefined, { renderer: 'canvas' });
    chartRef.current = chart;

    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(hostRef.current);

    return () => {
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  // Apply option (notMerge so removed series/axes don't linger).
  useEffect(() => {
    chartRef.current?.setOption(option, { notMerge: true, lazyUpdate: true });
  }, [option]);

  // Group sync for cross-panel tooltip + dataZoom. Clean up on unmount / group
  // change so a disposed chart doesn't linger in echarts' connected-group
  // registry (BUG-18). Defensive today — call sites pass static literals.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !group) return;
    chart.group = group;
    echarts.connect(group);
    return () => {
      chart.group = '';
      echarts.disconnect(group);
    };
  }, [group]);

  // Event binding — replace handlers when the map identity changes.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !onEvents) return;
    const entries = Object.entries(onEvents);
    for (const [name, handler] of entries) chart.on(name, handler);
    return () => {
      for (const [name, handler] of entries) chart.off(name, handler);
    };
  }, [onEvents]);

  return <div ref={hostRef} style={{ width: '100%', height }} />;
}
