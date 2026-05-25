import { useUnitsStore } from '@/store/units';
import { useClockStore } from '@/store/clockFormat';
import {
  fmtAlt as _fmtAlt,
  fmtSpd as _fmtSpd,
  fmtDist as _fmtDist,
  fmtTs as _fmtTs,
  fmtDate as _fmtDate,
  fmtAxisTime as _fmtAxisTime,
  fmtAxisDate as _fmtAxisDate,
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
  const clockFormat = useClockStore((s) => s.clockFormat);
  return {
    units,
    clockFormat,
    fmtAlt: (ft: number | null | undefined, showUnit = true) => _fmtAlt(ft, units, showUnit),
    fmtSpd: (kts: number | null | undefined, showUnit = true) => _fmtSpd(kts, units, showUnit),
    fmtDist: (nm: number | null | undefined, showUnit = true) => _fmtDist(nm, units, showUnit),
    fmtTs: (epoch: number | null | undefined) => _fmtTs(epoch, clockFormat),
    fmtDate: _fmtDate,
    fmtAxisTime: (epoch: number | null | undefined) => _fmtAxisTime(epoch, clockFormat),
    fmtAxisDate: _fmtAxisDate,
    altLabel: () => _altLabel(units),
    spdLabel: () => _spdLabel(units),
    distLabel: () => _distLabel(units),
  };
}
