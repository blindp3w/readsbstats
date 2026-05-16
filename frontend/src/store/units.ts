import { create } from 'zustand';

// Ports the v1 unit selector (static/js/units.js) to Zustand.
//
// Storage key MUST remain `rsbs_units` — users with saved preferences
// shouldn't lose them on cutover. Old "aero" value migrated to
// "aeronautical" once at load time (matches improvements.md #121).

export type UnitSystem = 'metric' | 'imperial' | 'aeronautical';

const KEY = 'rsbs_units';

function readStoredUnits(): UnitSystem {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw === 'aero') {
      // One-time migration kept for users who saved before #121.
      localStorage.setItem(KEY, 'aeronautical');
      return 'aeronautical';
    }
    if (raw === 'metric' || raw === 'imperial' || raw === 'aeronautical') return raw;
  } catch {
    /* localStorage unavailable (e.g. private mode) — fall through */
  }
  return 'metric';
}

interface UnitsStore {
  units: UnitSystem;
  setUnits: (u: UnitSystem) => void;
}

export const useUnitsStore = create<UnitsStore>((set) => ({
  units: readStoredUnits(),
  setUnits: (u) => {
    try {
      localStorage.setItem(KEY, u);
    } catch {
      /* ignore */
    }
    set({ units: u });
  },
}));

// Convenience accessor for code outside React (e.g. CSV download formatting).
export function getUnits(): UnitSystem {
  return useUnitsStore.getState().units;
}
