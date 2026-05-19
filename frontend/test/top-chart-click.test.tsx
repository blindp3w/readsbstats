import { vi, describe, it, expect, beforeEach } from 'vitest';
import { render, fireEvent, act, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

// Override the global EChart stub: capture the onEvents prop so we can drive
// it from the test and assert the resulting React Router navigation.
let lastOnEvents: Record<string, (...args: unknown[]) => void> | null = null;
vi.mock('@/components/charts/EChart', () => ({
  EChart: (props: any) => {
    lastOnEvents = props.onEvents ?? null;
    return null;
  },
}));

import { TopChart } from '@/components/charts/TopChart';

function Probe() {
  const l = useLocation();
  return <div data-testid="loc">{l.pathname}</div>;
}

const visitorsProps = {
  loading: false,
  frequent_aircraft: [
    {
      icao_hex: 'abc123',
      registration: 'SP-LWA',
      aircraft_type: 'B789',
      flights: 9,
    } as any,
  ],
};

const aircraftProps = {
  loading: false,
  top_aircraft_types: [{ type: 'A320', type_desc: 'narrow', flights: 5 } as any],
};

describe('TopChart click navigation', () => {
  beforeEach(() => {
    lastOnEvents = null;
  });

  it('navigates on visitors-view click → /aircraft/:icao', () => {
    const { getByTestId } = render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route
            path="/"
            element={
              <>
                <TopChart {...visitorsProps} />
                <Probe />
              </>
            }
          />
          <Route path="/aircraft/:icao" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    // Switch to the Visitors tab — Radix ToggleGroupItem renders as a button.
    act(() => {
      fireEvent.click(screen.getByText('Visitors'));
    });
    expect(lastOnEvents).not.toBeNull();
    act(() => {
      lastOnEvents!.click({ data: { icao_hex: 'abc123', value: 9 } });
    });
    expect(getByTestId('loc').textContent).toBe('/aircraft/abc123');
  });

  it('does not navigate when the active view is not Visitors', () => {
    const { getByTestId } = render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route
            path="/"
            element={
              <>
                <TopChart {...aircraftProps} />
                <Probe />
              </>
            }
          />
          <Route path="/aircraft/:icao" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(lastOnEvents).not.toBeNull();
    act(() => {
      lastOnEvents!.click({ data: { value: 5, name: 'A320' } });
    });
    expect(getByTestId('loc').textContent).toBe('/');
  });

  it('safely handles a click payload missing icao_hex on the visitors view', () => {
    const { getByTestId } = render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route
            path="/"
            element={
              <>
                <TopChart {...visitorsProps} />
                <Probe />
              </>
            }
          />
          <Route path="/aircraft/:icao" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
    act(() => {
      fireEvent.click(screen.getByText('Visitors'));
    });
    act(() => {
      lastOnEvents!.click({ data: { value: 9 } });
    });
    expect(getByTestId('loc').textContent).toBe('/');
  });
});
