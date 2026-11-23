import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MessageList } from '@/components/vdl2/MessageList';
import type { DecodedAcars } from '@/lib/acarsDecode';
import type { Vdl2Message } from '@/lib/types';

const msg = { id: 1, ts: 1_749_000_000, label: 'H1', body: 'RAW' } as Vdl2Message;

function renderWith(decoded: DecodedAcars) {
  return render(
    <TooltipProvider delayDuration={0}>
      <MessageList messages={[msg]} decode={() => decoded} />
    </TooltipProvider>,
  );
}

describe('MessageList structured decoded items', () => {
  it('renders position, route, figures and landing table', () => {
    renderWith({
      description: 'Position Report',
      items: [
        { type: 'aircraft_position', code: 'POS', label: 'Aircraft Position', value: '52.135 N, 20.028 E' },
        { type: 'aircraft_route', code: 'ROUTE', label: 'Aircraft Route', value: 'ABESI@08:53:21 > ASKIL > XIMAL' },
        { type: 'gross_weight', code: 'GWT', label: 'Gross Weight', value: '123400 pounds' },
        { type: 'fuel_on_board', code: 'FOB', label: 'Fuel On Board', value: '12400' },
        { type: 'runway_landing_data', code: 'RWYLDG', label: 'Runway Landing Data', value: '11: 2800 feet' },
        { type: 'runway_landing_data', code: 'RWYLDG', label: 'Runway Landing Data', value: '29: 3690 feet' },
        { type: 'icao', code: 'ORG', label: 'Origin', value: 'EPWA' },
      ],
    });
    expect(screen.getByTestId('vdl2-pos-chip').textContent).toContain('52.135°N 20.028°E');
    const route = screen.getByTestId('vdl2-route');
    expect(within(route).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'ABESI@08:53:21',
      '›ASKIL',
      '›XIMAL',
    ]);
    const figs = screen.getAllByTestId('vdl2-figure').map((f) => f.textContent);
    expect(figs).toEqual(['Gross Weight 123,400 lb', 'Fuel On Board 12,400']);
    const landing = screen.getByTestId('vdl2-landing');
    expect(within(landing).getAllByRole('row')).toHaveLength(3); // header + 2 runways
    expect(landing.textContent).toContain('2800 feet');
    // unknown type keeps the plain chip
    expect(screen.getByText('Origin')).toBeTruthy();
    expect(screen.getByText('EPWA')).toBeTruthy();
    expect(screen.getByText('RAW')).toBeTruthy();
  });

  it('falls back to plain chips for unknown types and malformed values', () => {
    renderWith({
      description: 'Weird',
      items: [
        { type: 'mystery', label: 'Mystery', value: '<b>x</b>' },
        { type: 'aircraft_position', label: 'Aircraft Position', value: 'not a fix' },
        { label: 'No type', value: 'v' },
      ],
    });
    expect(screen.queryByTestId('vdl2-pos-chip')).toBeNull();
    expect(screen.getByText('<b>x</b>')).toBeTruthy(); // escaped text, not markup
    expect(screen.getByText('not a fix')).toBeTruthy();
    expect(screen.getByText('No type')).toBeTruthy();
  });
});
