import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { RoutePosition } from '@/lib/routeSegments';

// RouteMap is globally mocked in test/setup.ts — this suite tests the real one
// against a fake react-map-gl <Map>. The fake mirrors react-map-gl 8: the
// MapLibre instance (and so the forwarded ref) only exists after an async init,
// and `onLoad` fires after that. The live bug: the fitBounds effect ran while
// mapRef was still null, cached the fit key anyway, and never fitted — the route
// map stayed centred on the first point at zoom 9.
vi.unmock('@/components/RouteMap');

const fitBounds = vi.fn();

vi.mock('@/lib/maplibreWorker', () => ({}));
vi.mock('@/hooks/useBasemapStyle', () => ({
  useBasemapStyle: () => ({ version: 8, sources: {}, layers: [] }),
}));
vi.mock('react-map-gl/maplibre', async () => {
  const React = await import('react');
  const Map = React.forwardRef<unknown, { onLoad?: () => void; children?: ReactNode }>(
    function FakeMap({ onLoad, children }, ref) {
      const [created, setCreated] = React.useState(false);
      React.useImperativeHandle(ref, () => (created ? { fitBounds } : null), [created]);
      React.useEffect(() => {
        const t = setTimeout(() => setCreated(true), 0);
        return () => clearTimeout(t);
      }, []);
      React.useEffect(() => {
        if (created) onLoad?.();
      }, [created, onLoad]);
      return <div data-testid="fake-map">{children}</div>;
    },
  );
  const Pass = ({ children }: { children?: ReactNode }) => <>{children}</>;
  return {
    Map,
    Source: Pass,
    Layer: () => null,
    Marker: Pass,
    AttributionControl: () => null,
    NavigationControl: () => null,
  };
});

const { default: RouteMap } = await import('@/components/RouteMap');

const track = (pts: [number, number][]): RoutePosition[] =>
  pts.map(([lat, lon], i) => ({ ts: i, lat, lon, source_type: 'adsb_icao' }));

// [lat, lon] in, as the API delivers them.
const EAST = track([
  [52.3, 20.9],
  [52.2, 21.4],
  [52.1, 22.0],
]);

describe('RouteMap fitBounds', () => {
  beforeEach(() => fitBounds.mockClear());

  it('fits the whole route once the map has loaded', async () => {
    render(<RouteMap positions={EAST} receiverLat={52.22} receiverLon={20.94} />);
    await waitFor(() => expect(fitBounds).toHaveBeenCalledTimes(1));
    const [bounds, opts] = fitBounds.mock.calls[0];
    // [[minLng, minLat], [maxLng, maxLat]] covering every plotted point.
    expect(bounds).toEqual([
      [20.9, 52.1],
      [22.0, 52.3],
    ]);
    expect(opts).toMatchObject({ padding: 20 });
  });

  it('does not refit when the same track re-renders with a new array identity', async () => {
    const { rerender } = render(
      <RouteMap positions={EAST} receiverLat={null} receiverLon={null} />,
    );
    await waitFor(() => expect(fitBounds).toHaveBeenCalledTimes(1));
    rerender(<RouteMap positions={[...EAST]} receiverLat={null} receiverLon={null} />);
    await new Promise((r) => setTimeout(r, 10));
    expect(fitBounds).toHaveBeenCalledTimes(1);
  });

  it('refits when the track changes', async () => {
    const { rerender } = render(
      <RouteMap positions={EAST} receiverLat={null} receiverLon={null} />,
    );
    await waitFor(() => expect(fitBounds).toHaveBeenCalledTimes(1));
    const NORTH = track([
      [52.2, 20.9],
      [53.0, 21.0],
    ]);
    rerender(<RouteMap positions={NORTH} receiverLat={null} receiverLon={null} />);
    await waitFor(() => expect(fitBounds).toHaveBeenCalledTimes(2));
    expect(fitBounds.mock.calls[1][0]).toEqual([
      [20.9, 52.2],
      [21.0, 53.0],
    ]);
  });
});
