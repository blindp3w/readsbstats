// lib/basemap.ts + hooks/useBasemapStyle.ts — the shared CARTO Dark Matter
// basemap used by LiveMap and RouteMap.
//
// Contracts pinned here:
//  - darkBasemapStyle(tiles): one raster source fed by exactly those tile
//    URLs, the background + lifted-blacks raster layer, OSM/CARTO attribution.
//  - useBasemapStyle: null while /api/map/basemap is loading (the map waits
//    rather than firing keyless tile requests that come back as CARTO's
//    "API KEY REQUIRED" placeholder); the server's tiles once loaded; the
//    keyless fallback when the request fails or returns no usable https URL.

import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest';
import React from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { RasterSourceSpecification } from 'maplibre-gl';

vi.mock('@/lib/api', () => ({ apiJson: vi.fn() }));

import { apiJson } from '@/lib/api';
import { CARTO_KEYLESS_TILES, darkBasemapStyle } from '@/lib/basemap';
import { useBasemapStyle } from '@/hooks/useBasemapStyle';

const apiJsonMock = apiJson as Mock;

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const tilesOf = (style: ReturnType<typeof darkBasemapStyle> | null) =>
  (style?.sources['carto-dark'] as RasterSourceSpecification | undefined)?.tiles;

const KEYED = 'https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png?key=abc123_DEF';

beforeEach(() => {
  apiJsonMock.mockReset();
});

describe('darkBasemapStyle', () => {
  it('feeds the raster source exactly the given tiles, with attribution', () => {
    const style = darkBasemapStyle([KEYED]);
    const src = style.sources['carto-dark'] as RasterSourceSpecification;
    expect(src.type).toBe('raster');
    expect(src.tiles).toEqual([KEYED]);
    expect(src.attribution).toContain('OpenStreetMap');
    expect(src.attribution).toContain('CARTO');
    expect(style.layers.map((l) => l.id)).toEqual(['bg', 'carto-dark']);
  });

  it('keyless fallback lists all four legacy subdomains', () => {
    expect(CARTO_KEYLESS_TILES).toHaveLength(4);
    for (const s of 'abcd') {
      expect(CARTO_KEYLESS_TILES).toContain(
        `https://${s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png`,
      );
    }
  });
});

describe('useBasemapStyle', () => {
  it('is null while loading, then uses the server tiles', async () => {
    let resolve!: (v: unknown) => void;
    apiJsonMock.mockReturnValue(new Promise((r) => (resolve = r)));
    const { result } = renderHook(() => useBasemapStyle(), { wrapper: makeWrapper() });
    expect(result.current).toBeNull();
    resolve({ tiles: [KEYED], keyed: true });
    await waitFor(() => expect(tilesOf(result.current)).toEqual([KEYED]));
    expect(apiJsonMock).toHaveBeenCalledWith('map/basemap');
  });

  it('falls back to keyless tiles when the request fails', async () => {
    apiJsonMock.mockRejectedValue(new Error('HTTP 500'));
    const { result } = renderHook(() => useBasemapStyle(), { wrapper: makeWrapper() });
    await waitFor(() => expect(tilesOf(result.current)).toEqual(CARTO_KEYLESS_TILES));
  });

  it('drops non-https tile URLs and falls back when none remain', async () => {
    apiJsonMock.mockResolvedValue({ tiles: ['http://evil.example/{z}/{x}/{y}.png', 42], keyed: true });
    const { result } = renderHook(() => useBasemapStyle(), { wrapper: makeWrapper() });
    await waitFor(() => expect(tilesOf(result.current)).toEqual(CARTO_KEYLESS_TILES));
  });
});
