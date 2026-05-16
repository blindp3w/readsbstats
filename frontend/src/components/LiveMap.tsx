import { useEffect, useMemo, useRef } from 'react';
import {
  MapContainer,
  TileLayer,
  CircleMarker,
  Marker,
  Polyline,
  Polygon,
  ZoomControl,
  useMap,
} from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import 'leaflet.heat';
import { aircraftIcon, getIconType } from '@/lib/aircraftIcon';

// Live aircraft layer for /v2/map. Receives the latest snapshot from the
// parent; renders aircraft as SVG divIcons + an optional trail polyline.
//
// Heavy: lazy-loaded by Map.tsx so other pages don't import Leaflet.

export interface Aircraft {
  flight_id: number;
  icao_hex: string;
  callsign: string | null;
  registration: string | null;
  aircraft_type: string | null;
  category: string | null;
  primary_source: string | null;
  flags: number;
  origin_icao: string | null;
  dest_icao: string | null;
  lat: number | null;
  lon: number | null;
  ts: number;
  alt_baro: number | null;
  gs: number | null;
  track: number | null;
  source_type: string | null;
  seconds_ago: number;
  trail: [number, number, number][];
}

interface Props {
  aircraft: Aircraft[];
  receiverLat: number | null;
  receiverLon: number | null;
  selectedFlightId: number | null;
  onSelect: (a: Aircraft) => void;
  // Initial view; we only auto-fit on first load to avoid yanking the user
  // around mid-pan.
  initialCenter: [number, number] | null;
  // Optional overlays — when undefined / empty the layer doesn't render.
  heatmapPoints?: [number, number, number][];
  coveragePolygon?: [number, number][];
}

// Leaflet.heat injects L.heatLayer onto the L global. Wrapper that mounts the
// layer once, updates on data change, removes on unmount.
function HeatmapLayer({ points }: { points: [number, number, number][] }) {
  const map = useMap();
  const layerRef = useRef<L.Layer | null>(null);
  useEffect(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const heat = (L as any).heatLayer(points, {
      radius: 18,
      blur: 25,
      maxZoom: 12,
      minOpacity: 0.25,
    });
    heat.addTo(map);
    layerRef.current = heat;
    return () => {
      if (layerRef.current) {
        map.removeLayer(layerRef.current);
        layerRef.current = null;
      }
    };
  }, [map, points]);
  return null;
}

function FirstFitOnce({ center }: { center: [number, number] | null }) {
  const map = useMap();
  const done = useRef(false);
  useEffect(() => {
    if (done.current || !center) return;
    map.setView(center, 8);
    done.current = true;
  }, [center, map]);
  return null;
}

export default function LiveMap({
  aircraft,
  receiverLat,
  receiverLon,
  selectedFlightId,
  onSelect,
  initialCenter,
  heatmapPoints,
  coveragePolygon,
}: Props) {
  const fallback: [number, number] = useMemo(
    () => (receiverLat != null && receiverLon != null ? [receiverLat, receiverLon] : [52, 21]),
    [receiverLat, receiverLon],
  );

  return (
    <MapContainer
      center={initialCenter ?? fallback}
      zoom={8}
      preferCanvas={true}
      className="h-full w-full"
      data-testid="map-container"
      // Leaflet's default zoom control sits at top-left and overlaps our
      // Live/Rewind overlay. Reposition to the bottom-right corner — visible
      // but out of the way of every other overlay.
      zoomControl={false}
    >

      <ZoomControl position="bottomright" />
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        className="map-tiles-dark"
      />

      {heatmapPoints && heatmapPoints.length > 0 && <HeatmapLayer points={heatmapPoints} />}

      {coveragePolygon && coveragePolygon.length >= 3 && (
        <Polygon
          positions={coveragePolygon as L.LatLngExpression[]}
          pathOptions={{
            color: '#a855f7',
            weight: 2,
            fillColor: '#a855f7',
            fillOpacity: 0.08,
          }}
        />
      )}

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

      {aircraft.map((ac) => {
        if (ac.lat == null || ac.lon == null) return null;
        const icon = aircraftIcon(
          ac.track,
          ac.flags,
          getIconType(ac.category, ac.aircraft_type),
        );
        const isSelected = ac.flight_id === selectedFlightId;
        return (
          <AircraftMarker
            key={ac.flight_id}
            ac={ac}
            icon={icon}
            isSelected={isSelected}
            onSelect={onSelect}
          />
        );
      })}

      {/* Trail for selected aircraft only — drawing trails for all 300
          markers spikes the JS thread on every poll. */}
      {selectedFlightId != null &&
        (() => {
          const sel = aircraft.find((a) => a.flight_id === selectedFlightId);
          if (!sel || sel.trail.length < 2) return null;
          const pts: L.LatLngExpression[] = sel.trail.map(([lat, lon]) => [lat, lon]);
          return (
            <Polyline
              positions={pts}
              pathOptions={{ color: '#5b9af9', weight: 2, opacity: 0.8 }}
            />
          );
        })()}

      <FirstFitOnce center={initialCenter ?? fallback} />
    </MapContainer>
  );
}

function AircraftMarker({
  ac,
  icon,
  isSelected,
  onSelect,
}: {
  ac: Aircraft;
  icon: L.DivIcon;
  isSelected: boolean;
  onSelect: (a: Aircraft) => void;
}) {
  // useMemo on event handler avoids creating fresh closures every render —
  // Marker re-binds events when handlers change identity.
  const handlers = useMemo(
    () => ({
      click: () => onSelect(ac),
    }),
    [ac, onSelect],
  );
  return (
    <Marker
      position={[ac.lat as number, ac.lon as number]}
      icon={icon}
      eventHandlers={handlers}
      zIndexOffset={isSelected ? 1000 : 0}
    />
  );
}
