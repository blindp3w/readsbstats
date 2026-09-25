import { useQuery } from '@tanstack/react-query';
import type { StyleSpecification } from 'maplibre-gl';
import { apiJson } from '@/lib/api';
import { CARTO_KEYLESS_TILES, darkBasemapStyle, type BasemapResponse } from '@/lib/basemap';

// Basemap style for LiveMap / RouteMap. Returns null while /api/map/basemap
// is in flight — callers hold the <Map> back rather than firing keyless tile
// requests that would render CARTO's placeholder. Seeded in App.tsx on boot
// (shared ['basemap'] key) so it is normally warm before a map mounts. Falls
// back to the keyless tiles on error or when no https URL survives the check
// (the URLs land in a MapLibre source, so reject anything that isn't https).
export const basemapQuery = {
  queryKey: ['basemap'],
  queryFn: () => apiJson<BasemapResponse>('map/basemap'),
  staleTime: Infinity,
};

export function useBasemapStyle(): StyleSpecification | null {
  const { data, isError } = useQuery(basemapQuery);
  if (isError) return darkBasemapStyle(CARTO_KEYLESS_TILES);
  if (!data) return null;
  const tiles = (Array.isArray(data.tiles) ? data.tiles : []).filter(
    (t): t is string => typeof t === 'string' && t.startsWith('https://'),
  );
  return darkBasemapStyle(tiles.length > 0 ? tiles : CARTO_KEYLESS_TILES);
}
