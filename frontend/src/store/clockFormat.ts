import { create } from 'zustand';

// Clock-format store mirrors store/units.ts. The backend exposes its
// default via RSBS_TIME_FORMAT → /api/settings.time_format; App.tsx seeds
// this store on first boot ONLY when localStorage is empty, so a user's
// explicit local override (set in devtools or by a future UI toggle)
// always wins over the server-side default after first set.

export type ClockFormat = '24h' | '12h';

export const CLOCK_FORMAT_KEY = 'rsbs_clock_format';

export function hasStoredClockFormat(): boolean {
  try {
    return localStorage.getItem(CLOCK_FORMAT_KEY) !== null;
  } catch {
    return false;
  }
}

function readStored(): ClockFormat {
  try {
    const v = localStorage.getItem(CLOCK_FORMAT_KEY);
    if (v === '24h' || v === '12h') return v;
  } catch {
    /* localStorage unavailable (private mode) — fall through */
  }
  return '24h';
}

interface ClockStore {
  clockFormat: ClockFormat;
  setClockFormat: (f: ClockFormat) => void;
}

export const useClockStore = create<ClockStore>((set) => ({
  clockFormat: readStored(),
  setClockFormat: (f) => {
    try {
      localStorage.setItem(CLOCK_FORMAT_KEY, f);
    } catch {
      /* ignore */
    }
    set({ clockFormat: f });
  },
}));

// Snapshot accessor for non-render contexts (CSV export, fmtTs default, etc).
export function getClockFormat(): ClockFormat {
  return useClockStore.getState().clockFormat;
}
