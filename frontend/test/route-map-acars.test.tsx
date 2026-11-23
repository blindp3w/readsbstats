// RouteMap `acarsFixes`: distinct markers + popup + a legend entry, only when fixes
// exist; byte-for-byte the old output when the prop is absent/empty.
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { RoutePosition } from '@/lib/routeSegments';
import type { Vdl2Position } from '@/lib/types';
import { useUnitsStore } from '@/store/units';

vi.unmock('@/components/RouteMap');

vi.mock('@/lib/maplibreWorker', () => ({}));
vi.mock('@/hooks/useBasemapStyle', () => ({
  useBasemapStyle: () => ({ version: 8, sources: {}, layers: [] }),
}));
vi.mock('react-map-gl/maplibre', async () => {
  const React = await import('react');
  const Map = React.forwardRef<unknown, { children?: ReactNode }>(function FakeMap(
    { children },
    ref,
  ) {
    React.useImperativeHandle(ref, () => null, []);
    return <div data-testid="fake-map">{children}</div>;
  });
  return {
    Map,
    Source: ({ children }: { children?: ReactNode }) => <>{children}</>,
    Layer: () => null,
    Marker: ({
      children,
      onClick,
      longitude,
    }: {
      children?: ReactNode;
      onClick?: (e: unknown) => void;
      longitude: number;
    }) => (
      <div
        data-testid="fake-marker"
        data-lng={longitude}
        onClick={() => onClick?.({ originalEvent: { stopPropagation: () => {} } })}
      >
        {children}
      </div>
    ),
    Popup: ({ children, onClose }: { children?: ReactNode; onClose: () => void }) => (
      <div data-testid="fake-popup">
        <button data-testid="popup-close" onClick={onClose} />
        {children}
      </div>
    ),
    AttributionControl: () => null,
    NavigationControl: () => null,
  };
});

const { default: RouteMap } = await import('@/components/RouteMap');

const track: RoutePosition[] = [
  { ts: 0, lat: 52.3, lon: 20.9, source_type: 'adsb_icao' },
  { ts: 1, lat: 52.2, lon: 21.4, source_type: 'adsb_icao' },
];
const fixes: Vdl2Position[] = [
  {
    lat: 52.25,
    lon: 21.1,
    icao_hex: '4ca123',
    ts: 1_700_000_000,
    label: 'H1',
    precise: true,
    source: 'h1-abs',
    alt: 35000,
  },
  { lat: 52.21, lon: 21.3, icao_hex: '4ca123', ts: 1_700_000_300, label: '16', precise: true, source: 'label' },
];

describe('RouteMap acarsFixes', () => {
  it('renders no ACARS markers or legend when the prop is absent', () => {
    render(<RouteMap positions={track} receiverLat={null} receiverLon={null} />);
    expect(screen.queryAllByTestId('route-acars-fix')).toHaveLength(0);
    expect(screen.queryByTestId('route-acars-legend')).toBeNull();
    // start + end only
    expect(screen.getAllByTestId('fake-marker')).toHaveLength(2);
  });

  it('renders no legend for an empty fix list', () => {
    render(<RouteMap positions={track} receiverLat={null} receiverLon={null} acarsFixes={[]} />);
    expect(screen.queryByTestId('route-acars-legend')).toBeNull();
  });

  it('renders one distinct marker per fix and a legend entry', () => {
    render(<RouteMap positions={track} receiverLat={null} receiverLon={null} acarsFixes={fixes} />);
    expect(screen.getAllByTestId('route-acars-fix')).toHaveLength(2);
    expect(screen.getByTestId('route-acars-legend').textContent).toContain('ACARS position');
  });

  it('skips fixes without coordinates', () => {
    const bad = [{ ...fixes[0], lat: null as unknown as number }, fixes[1]];
    render(<RouteMap positions={track} receiverLat={null} receiverLon={null} acarsFixes={bad} />);
    expect(screen.getAllByTestId('route-acars-fix')).toHaveLength(1);
  });

  it('opens a popup with source, time and altitude on click, and closes it', () => {
    useUnitsStore.setState({ units: 'aeronautical' });
    render(<RouteMap positions={track} receiverLat={null} receiverLon={null} acarsFixes={fixes} />);
    expect(screen.queryByTestId('fake-popup')).toBeNull();
    fireEvent.click(screen.getAllByTestId('route-acars-fix')[0]);
    const popup = screen.getByTestId('fake-popup');
    expect(popup.textContent).toContain('H1 ABS report');
    expect(popup.textContent).toContain('35,000 ft');
    fireEvent.click(screen.getByTestId('popup-close'));
    expect(screen.queryByTestId('fake-popup')).toBeNull();
  });
});
