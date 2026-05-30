import { describe, it, expect, beforeEach } from 'vitest';

import { useClockStore } from '@/store/clockFormat';
import { buildPanelOption } from '@/pages/metricsCharts';
import {
  fmtTs as bareFmtTs,
  fmtAxisTime as bareFmtAxisTime,
  fmtAxisDate as bareFmtAxisDate,
} from '@/lib/format';

// `buildPanelOption` accepts `fmtTs` as a parameter — pass the bare helper
// from lib/format so the test exercises the same clock-format propagation
// path the React component takes (useFormat → fmtTs → bareFmtTs(clockFormat)).

const sampleResp = {
  bucket_seconds: 0,
  metrics: ['signal'],
  data: [[1_747_573_200], [-15]],
};

describe('Metrics axis time formatter respects RSBS_TIME_FORMAT', () => {
  beforeEach(() => {
    useClockStore.getState().setClockFormat('24h');
  });

  it('24h: rendered tick contains no AM/PM marker', () => {
    const fmtAxisTime = (e: number) => bareFmtAxisTime(e, '24h');
    const fmtTs = (e: number) => bareFmtTs(e, '24h');
    const opt = buildPanelOption(
      sampleResp,
      ['signal'],
      ['#1'],
      fmtAxisTime,
      bareFmtAxisDate,
      fmtTs,
    );
    const out = ((opt.xAxis as any).axisLabel.formatter as (v: number) => string)(
      1_747_573_200_000,
    );
    expect(out).not.toMatch(/AM|PM|a\.m\.|p\.m\./i);
  });

  it('12h: rendered tick contains an AM/PM marker', () => {
    const fmtAxisTime = (e: number) => bareFmtAxisTime(e, '12h');
    const fmtTs = (e: number) => bareFmtTs(e, '12h');
    const opt = buildPanelOption(
      sampleResp,
      ['signal'],
      ['#1'],
      fmtAxisTime,
      bareFmtAxisDate,
      fmtTs,
    );
    const out = ((opt.xAxis as any).axisLabel.formatter as (v: number) => string)(
      1_747_573_200_000,
    );
    expect(out).toMatch(/AM|PM|a\.m\.|p\.m\./i);
  });
});
