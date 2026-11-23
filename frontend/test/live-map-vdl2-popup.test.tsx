// LiveMap VDL2 dots: click opens a popup (and only a popup); aircraft markers still
// select (and only select). react-map-gl is replaced by a fake that records the Map
// props and lets the test fire a map click carrying `features`.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import type { ReactNode } from 'react';
import type { Vdl2Position } from '@/lib/types';
import { useUnitsStore } from '@/store/units';

vi.unmock('@/components/LiveMap');

let mapProps: Record<string, any> = {};
const layerIds: string[] = [];

vi.mock('@/lib/maplibreWorker', () => ({}));
vi.mock('@/hooks/useBasemapStyle', () => ({
  useBasemapStyle: () => ({ version: 8, sources: {}, layers: [] }),
}));
vi.mock('react-map-gl/maplibre', async () => {
  const React = await import('react');
  const Map = React.forwardRef<unknown, { children?: ReactNode } & Record<string, any>>(
    function FakeMap(props, ref) {
      mapProps = props;
      React.useImperativeHandle(ref, () => ({ jumpTo: () => {}, getMap: () => null }), []);
      return <div data-testid="fake-map">{props.children}</div>;
    },
  );
  return {
    Map,
    Source: ({ children }: { children?: ReactNode }) => <>{children}</>,
    Layer: ({ id }: { id: string }) => {
      layerIds.push(id);
      return null;
    },
    Marker: ({
      children,
      onClick,
    }: {
      children?: ReactNode;
      onClick?: (e: unknown) => void;
    }) => (
      <button
        data-testid="aircraft-marker"
        onClick={() => onClick?.({ originalEvent: { stopPropagation: () => {} } })}
      >
        {children}
      </button>
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

const { default: LiveMap } = await import('@/components/LiveMap');

const pts: Vdl2Position[] = [
  { lat: 52, lon: 21, icao_hex: '4ca123', ts: 1_700_000_000, label: '16', precise: true, source: 'label' },
  {
    lat: 53,
    lon: 22,
    icao_hex: '4ca999',
    ts: 1_700_000_100,
    label: 'H1',
    precise: true,
    source: 'h1-abs',
    alt: 36000,
    wind_dir: 270,
    wind_kt: 55,
  },
  { lat: 51, lon: 20, icao_hex: null, ts: null, label: null, precise: false, source: 'xid' },
];

const aircraft = [
  {
    flight_id: 7,
    icao_hex: 'abc123',
    callsign: 'TST1',
    registration: null,
    aircraft_type: null,
    category: null,
    primary_source: null,
    flags: 0,
    origin_icao: null,
    dest_icao: null,
    lat: 52,
    lon: 21,
    ts: 1,
    alt_baro: null,
    gs: null,
    track: 0,
    source_type: null,
    seconds_ago: 1,
    trail: [] as [number, number, number][],
  },
];

const clickDot = (idx: number | null) =>
  act(() => {
    mapProps.onClick({ features: idx == null ? [] : [{ properties: { idx } }] });
  });

describe('LiveMap VDL2 popup', () => {
  const onSelect = vi.fn();
  beforeEach(() => {
    onSelect.mockReset();
    layerIds.length = 0;
    useUnitsStore.setState({ units: 'aeronautical' });
  });
  const renderMap = (vdl2?: Vdl2Position[]) =>
    render(
      <LiveMap
        aircraft={aircraft}
        receiverLat={52}
        receiverLon={21}
        selectedFlightId={null}
        onSelect={onSelect}
        initialCenter={null}
        vdl2Positions={vdl2}
      />,
    );

  it('registers the hit layer as interactive only while VDL2 points exist', () => {
    renderMap(pts);
    expect(mapProps.interactiveLayerIds).toEqual(['vdl2-positions-hit']);
    expect(layerIds).toContain('vdl2-positions-hit');
  });

  it('has no interactive layers and no popup when the overlay is off', () => {
    renderMap(undefined);
    expect(mapProps.interactiveLayerIds).toBeUndefined();
    expect(layerIds).not.toContain('vdl2-positions-hit');
    clickDot(0);
    expect(screen.queryByTestId('fake-popup')).toBeNull();
  });

  it('opens a popup with source, label, time and extras — and does not select an aircraft', () => {
    renderMap(pts);
    clickDot(1);
    const popup = screen.getByTestId('fake-popup');
    expect(popup.textContent).toContain('H1 ABS report');
    expect(popup.textContent).toContain('4ca999');
    expect(popup.textContent).toContain('Label');
    expect(popup.textContent).toContain('H1');
    expect(popup.textContent).toContain('270°');
    expect(popup.textContent).toContain('36,000 ft');
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('flags coarse fixes', () => {
    renderMap(pts);
    clickDot(2);
    expect(screen.getByTestId('fake-popup').textContent).toContain('VDL2 link (coarse)');
  });

  it('closes on an empty-map click and via the popup close button', () => {
    renderMap(pts);
    clickDot(0);
    expect(screen.getByTestId('fake-popup')).toBeTruthy();
    clickDot(null);
    expect(screen.queryByTestId('fake-popup')).toBeNull();
    clickDot(0);
    fireEvent.click(screen.getByTestId('popup-close'));
    expect(screen.queryByTestId('fake-popup')).toBeNull();
  });

  it('an aircraft marker click selects the aircraft and opens no popup', () => {
    renderMap(pts);
    fireEvent.click(screen.getByTestId('aircraft-marker'));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('fake-popup')).toBeNull();
  });

  it('drops the popup when the overlay is switched off', () => {
    const { rerender } = renderMap(pts);
    clickDot(0);
    rerender(
      <LiveMap
        aircraft={aircraft}
        receiverLat={52}
        receiverLon={21}
        selectedFlightId={null}
        onSelect={onSelect}
        initialCenter={null}
      />,
    );
    expect(screen.queryByTestId('fake-popup')).toBeNull();
  });

  it('shows a pointer cursor over a dot', () => {
    renderMap(pts);
    act(() => mapProps.onMouseEnter());
    expect(mapProps.cursor).toBe('pointer');
    act(() => mapProps.onMouseLeave());
    expect(mapProps.cursor).toBe('');
  });
});
