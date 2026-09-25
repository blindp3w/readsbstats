import type { StyleSpecification } from 'maplibre-gl';

// CartoDB Dark Matter raster basemap (CC-BY 4.0), shared by LiveMap and
// RouteMap. Tile URLs come from /api/map/basemap so RSBS_CARTO_API_KEY stays
// in the server's env file — keyless requests now get CARTO's "API KEY
// REQUIRED" placeholder tiles.
//
// Keyless fallback (request failed / no key configured). MapLibre does not
// expand Leaflet's `{s}` subdomain placeholder; list the four explicitly.
// Mirrors api/map.py::_CARTO_KEYLESS_TILES.
export const CARTO_KEYLESS_TILES: string[] = ['a', 'b', 'c', 'd'].map(
  (s) => `https://${s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png`,
);

export interface BasemapResponse {
  tiles: string[];
  keyed: boolean;
}

// Background layer fills the canvas between tile loads so there is no white
// flash.
export function darkBasemapStyle(tiles: string[]): StyleSpecification {
  return {
    version: 8,
    sources: {
      'carto-dark': {
        type: 'raster',
        tiles,
        tileSize: 256,
        minzoom: 0,
        maxzoom: 20,
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
          'contributors, © <a href="https://carto.com/attributions">CARTO</a>',
      },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#0b0b0d' } },
      // Lift the blacks on Dark Matter — its default range bottoms out at near-
      // pitch-black which is hard to read at any zoom. raster-brightness-min
      // pushes the floor up to a mid-charcoal; raster-contrast pulls back a
      // touch so the lift doesn't wash the basemap out. Data layers (heatmap,
      // polylines, circles, markers) are unaffected — they paint on top of
      // the raster layer with their own paint properties.
      {
        id: 'carto-dark',
        type: 'raster',
        source: 'carto-dark',
        paint: {
          'raster-brightness-min': 0.18,
          'raster-contrast': -0.1,
        },
      },
    ],
  };
}
