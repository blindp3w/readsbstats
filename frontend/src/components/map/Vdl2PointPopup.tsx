import { Popup } from 'react-map-gl/maplibre';
import { useFormat } from '@/hooks/useFormat';
import { describeVdl2Point } from '@/lib/vdl2Points';
import type { Vdl2Position } from '@/lib/types';

// Click popup for one VDL2-derived position fix (live-map dot, flight route-map
// diamond). Shared by LiveMap and RouteMap, so it lives with the maps chunk.
// Text-only rendering (React escapes everything) — nothing from the feed reaches
// innerHTML. Popup chrome is MapLibre's light card, so colours are set explicitly.
export function Vdl2PointPopup({
  point,
  onClose,
  offset = 10,
}: {
  point: Vdl2Position;
  onClose: () => void;
  offset?: number;
}) {
  const { fmtTs, fmtAlt, fmtSpd } = useFormat();
  const d = describeVdl2Point(point, { fmtTs, fmtAlt, fmtSpd });
  return (
    <Popup
      longitude={point.lon}
      latitude={point.lat}
      offset={offset}
      closeOnClick={false}
      maxWidth="260px"
      onClose={onClose}
    >
      <div className="text-xs text-[#1c1917]" data-testid="vdl2-point-popup">
        <div className="font-semibold">
          {d.title}
          {!d.precise && <span className="ml-1 font-normal text-[#78716c]">~11 km</span>}
        </div>
        {point.icao_hex && <div className="font-mono text-[#57534e]">{point.icao_hex}</div>}
        <dl className="mt-1 grid grid-cols-[max-content_1fr] gap-x-2 gap-y-0.5">
          {d.rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-[#78716c]">{k}</dt>
              <dd className="tabnum">{v}</dd>
            </div>
          ))}
        </dl>
      </div>
    </Popup>
  );
}
