import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Vdl2MapLegend } from '@/components/map/Vdl2MapLegend';
import { MapLayersControl } from '@/components/map/MapLayersControl';

describe('Vdl2MapLegend', () => {
  it('names the precise/coarse encodings and the 60 min window', () => {
    render(<Vdl2MapLegend />);
    const t = screen.getByTestId('map-vdl2-legend').textContent;
    expect(t).toContain('last 60 min');
    expect(t).toContain('Precise');
    expect(t).toContain('Coarse');
  });

  it('the ACARS layer pill advertises the window in its tooltip', () => {
    render(
      <MapLayersControl
        showHeatmap={false}
        onToggleHeatmap={() => {}}
        showCoverage={false}
        onToggleCoverage={() => {}}
        sidebarOpen={false}
        onToggleSidebar={() => {}}
        variant="inline"
        showVdl2
        onToggleVdl2={() => {}}
      />,
    );
    expect(screen.getByTestId('map-toggle-vdl2').getAttribute('title')).toContain('60 min');
  });
});
