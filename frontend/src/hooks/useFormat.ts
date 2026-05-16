import { useUnitsStore } from '@/store/units';
import {
  fmtAlt as _fmtAlt,
  fmtSpd as _fmtSpd,
  fmtDist as _fmtDist,
  altLabel as _altLabel,
  spdLabel as _spdLabel,
  distLabel as _distLabel,
} from '@/lib/format';

// Reactive wrapper around unit-dependent format helpers. Subscribes to the
// Zustand units store so any component using these helpers re-renders when
// the user toggles units in the nav. Use this inside React render trees;
// for non-render contexts (CSV URLs, etc.) call the bare helpers in
// `lib/format.ts` with an explicit units argument.
export function useFormat() {
  const units = useUnitsStore((s) => s.units);
  return {
    units,
    fmtAlt: (ft: number | null | undefined, showUnit = true) => _fmtAlt(ft, units, showUnit),
    fmtSpd: (kts: number | null | undefined, showUnit = true) => _fmtSpd(kts, units, showUnit),
    fmtDist: (nm: number | null | undefined, showUnit = true) => _fmtDist(nm, units, showUnit),
    altLabel: () => _altLabel(units),
    spdLabel: () => _spdLabel(units),
    distLabel: () => _distLabel(units),
  };
}
