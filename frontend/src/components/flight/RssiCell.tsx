// RSSI cell for the Flight detail position log (M3.3). Renders the raw
// value as dim text + a tiny horizontal bar whose width is the value's
// position within this flight's RSSI [min, max] range, and whose color
// is green if value > flight median, amber otherwise.
//
// Brief intent: turn RSSI from "just another number" into the table's
// visual anchor. The bar is a relative signal (within this flight); the
// previous absolute-threshold color (rssiColor in Flight.tsx) is dropped
// so there's a single signal per cell.

interface Props {
  value: number | null;
  // Per-table aggregates (already filtered for NULLs).
  min: number;
  max: number;
  median: number;
}

export function RssiCell({ value, min, max, median }: Props) {
  if (value == null) {
    return (
      <div className="text-[var(--color-text-dim)]">
        <div>—</div>
      </div>
    );
  }

  // Range guard: if every observed RSSI is the same (or only one
  // non-null sample) the bar would be either 0% or NaN. Render a full
  // bar in the success color in that case — there's no meaningful
  // 'better-than-median' signal to convey. (For multi-sample flights
  // the strict > median keeps the existing 'green if better than typical'
  // semantics; tests pin this exact threshold.)
  const range = max - min;
  const widthPct = range > 0 ? Math.round(((value - min) / range) * 100) : 100;
  const isStrong = range === 0 ? true : value > median;
  const bg = isStrong ? 'var(--color-success)' : 'var(--color-warn)';

  return (
    <div data-testid="rssi-cell" data-strong={isStrong} className="text-[var(--color-text-dim)]">
      <div className="tabnum">{value.toFixed(1)} dB</div>
      <div
        aria-hidden="true"
        className="mt-0.5 h-[3px] rounded-sm"
        style={{ width: `${widthPct}%`, background: bg, minWidth: 2 }}
      />
    </div>
  );
}
