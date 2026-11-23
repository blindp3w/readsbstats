import { VDL2_MAP_WINDOW_MIN } from '@/lib/vdl2Points';

// Legend for the live-map ACARS overlay. Rendered by the Map page only while the layer
// is on. Swatches mirror the circle-layer paint in LiveMap (solid = precise fix,
// hollow ring = coarse ~0.1° XID link fix) and the window is the one the page requests.
export function Vdl2MapLegend() {
  return (
    <div
      data-testid="map-vdl2-legend"
      className="pointer-events-none absolute left-3 z-[10] rounded border border-[var(--color-border-default)] bg-[var(--color-surface)]/90 px-2.5 py-1.5 text-[11px] text-[var(--color-text-dim)] shadow-[var(--shadow-sm)] backdrop-blur"
      style={{ bottom: 'calc(var(--map-bar-height, 0px) + 36px)' }}
    >
      <div className="mb-1 font-medium text-[var(--color-text)]">
        ACARS positions · last {VDL2_MAP_WINDOW_MIN} min
      </div>
      <div className="flex items-center gap-3">
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-2 w-2 rounded-full border border-[#1c1917] bg-[#f59e0b]"
          />
          Precise
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-2 w-2 rounded-full border-[1.5px] border-[#f59e0b]/70"
          />
          Coarse (~11 km)
        </span>
      </div>
    </div>
  );
}
