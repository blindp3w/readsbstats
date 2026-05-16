import { useEffect, useMemo, useRef } from 'react';
import { MapContainer, TileLayer, CircleMarker, Polyline, useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';

// Flight route map. Each position becomes a vertex of a polyline; ADS-B vs
// MLAT segments are colored differently so users can spot multilateration
// gaps. Receiver location shown as a fixed marker.
//
// This module is lazy-loaded by Flight.tsx so other pages don't pull in
// Leaflet (~140 KB raw).

interface Position {
  ts: number;
  lat: number | null;
  lon: number | null;
  source_type: string | null;
}

interface Props {
  positions: Position[];
  receiverLat: number | null;
  receiverLon: number | null;
}

const ADSB_COLOR = '#22c55e';
const MLAT_COLOR = '#eab308';
const MIXED_COLOR = '#5b9af9';

function colorForSource(src: string | null | undefined): string {
  if (!src) return MIXED_COLOR;
  if (src.startsWith('adsb')) return ADSB_COLOR;
  if (src === 'mlat') return MLAT_COLOR;
  return MIXED_COLOR;
}

function FitBounds({ points }: { points: [number, number][] }) {
  const map = useMap();
  const last = useRef<string | null>(null);
  useEffect(() => {
    if (points.length === 0) return;
    const key = `${points.length}-${points[0][0]}-${points[points.length - 1][0]}`;
    if (key === last.current) return;
    last.current = key;
    const bounds = L.latLngBounds(points.map(([lat, lon]) => L.latLng(lat, lon)));
    map.fitBounds(bounds, { padding: [20, 20] });
  }, [points, map]);
  return null;
}

export default function RouteMap({ positions, receiverLat, receiverLon }: Props) {
  // Split positions into contiguous segments where source_type is constant —
  // gives us per-segment color without one polyline per pair-of-points.
  const segments = useMemo(() => {
    const segs: { points: [number, number][]; color: string }[] = [];
    let cur: { points: [number, number][]; color: string } | null = null;
    for (const p of positions) {
      if (p.lat == null || p.lon == null) continue;
      const color = colorForSource(p.source_type);
      if (!cur || cur.color !== color) {
        if (cur) segs.push(cur);
        cur = { points: [[p.lat, p.lon]], color };
      } else {
        cur.points.push([p.lat, p.lon]);
      }
    }
    if (cur) segs.push(cur);
    return segs;
  }, [positions]);

  const allPoints = useMemo(
    () => segments.flatMap((s) => s.points),
    [segments],
  );

  if (allPoints.length === 0 && receiverLat == null) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-[var(--color-text-dim)]">
        no positions to plot
      </div>
    );
  }

  const center: [number, number] =
    allPoints[0] ??
    (receiverLat != null && receiverLon != null ? [receiverLat, receiverLon] : [0, 0]);

  return (
    <MapContainer
      center={center}
      zoom={9}
      scrollWheelZoom={false}
      className="h-full w-full rounded"
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        // CSS filter darkens the tiles to match the dark UI.
        className="map-tiles-dark"
      />
      {segments.map((s, i) => (
        <Polyline
          key={i}
          positions={s.points as L.LatLngExpression[]}
          pathOptions={{ color: s.color, weight: 2, opacity: 0.85 }}
        />
      ))}
      {receiverLat != null && receiverLon != null && (
        <CircleMarker
          center={[receiverLat, receiverLon]}
          radius={6}
          pathOptions={{
            color: '#fff',
            weight: 2,
            fillColor: '#5b9af9',
            fillOpacity: 1,
          }}
        />
      )}
      {allPoints.length > 0 && <FitBounds points={allPoints} />}
    </MapContainer>
  );
}
