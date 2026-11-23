// Pure model for the OOOI block-time timeline (OUT -> OFF -> ON -> IN).
// Inputs are the raw "HHMM" UTC strings from the API (any may be null or
// malformed). Only known points are emitted; a connector between two points
// carries a name + duration only when they are the canonical adjacent pair
// (OUT-OFF taxi out, OFF-ON airborne, ON-IN taxi in) AND the times are in order.

export type OooiKey = 'out' | 'off' | 'on' | 'in';

export interface TimelinePoint {
  type: 'point';
  key: OooiKey;
  label: string; // OUT / OFF / ON / IN
  time: string; // raw API string
  minutes: number | null; // minutes since 00:00 UTC; null if not a valid HHMM
}

export interface TimelineConnector {
  type: 'connector';
  name: 'Taxi out' | 'Airborne' | 'Taxi in' | null;
  duration: number | null; // minutes
  airborne: boolean;
}

export type TimelineItem = TimelinePoint | TimelineConnector;

const ORDER: { key: OooiKey; label: string }[] = [
  { key: 'out', label: 'OUT' },
  { key: 'off', label: 'OFF' },
  { key: 'on', label: 'ON' },
  { key: 'in', label: 'IN' },
];

// An airborne leg may legitimately cross midnight UTC (OFF 2350 -> ON 0130);
// taxi legs never do. Anything above this cap is treated as bad data.
const MAX_AIRBORNE_MIN = 18 * 60;
const MAX_TAXI_MIN = 6 * 60;

export function hhmmToMinutes(t: string | null | undefined): number | null {
  if (!t || !/^\d{4}$/.test(t)) return null;
  const h = Number(t.slice(0, 2));
  const m = Number(t.slice(2));
  return h < 24 && m < 60 ? h * 60 + m : null;
}

export function formatDuration(min: number): string {
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

const LEGS: Record<string, { name: TimelineConnector['name']; airborne: boolean }> = {
  'out>off': { name: 'Taxi out', airborne: false },
  'off>on': { name: 'Airborne', airborne: true },
  'on>in': { name: 'Taxi in', airborne: false },
};

export function buildTimeline(times: Record<OooiKey, string | null | undefined>): TimelineItem[] {
  const known: TimelinePoint[] = ORDER.filter((o) => !!times[o.key]).map((o) => ({
    type: 'point' as const,
    key: o.key,
    label: o.label,
    time: times[o.key] as string,
    minutes: hhmmToMinutes(times[o.key]),
  }));
  const out: TimelineItem[] = [];
  known.forEach((p, i) => {
    if (i > 0) {
      const prev = known[i - 1];
      const leg = LEGS[`${prev.key}>${p.key}`];
      let duration: number | null = null;
      if (leg && prev.minutes !== null && p.minutes !== null) {
        let d = p.minutes - prev.minutes;
        if (d < 0 && leg.airborne) d += 1440; // crossed midnight UTC
        const max = leg.airborne ? MAX_AIRBORNE_MIN : MAX_TAXI_MIN;
        if (d >= 0 && d <= max) duration = d;
      }
      out.push({
        type: 'connector',
        name: leg && duration !== null ? leg.name : null,
        duration,
        airborne: !!leg?.airborne,
      });
    }
    out.push(p);
  });
  return out;
}

// Text alternative for the whole bar (screen readers; the visual is aria-hidden).
export function timelineSummary(items: TimelineItem[], fmtTime: (t: string) => string): string {
  return items
    .map((it) => {
      if (it.type === 'point') return `${it.label} ${fmtTime(it.time)}`;
      return it.name && it.duration !== null ? `${it.name} ${formatDuration(it.duration)}` : '';
    })
    .filter(Boolean)
    .join(', ');
}
