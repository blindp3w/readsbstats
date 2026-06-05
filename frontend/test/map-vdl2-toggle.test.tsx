/**
 * VDL2 map overlay toggle (Phase C). The "ACARS" layer pill renders only when
 * the parent wires `onToggleVdl2` (i.e. the feature is on AND vdl2.db is
 * queryable). With VDL2 off the parent omits the prop and no pill appears, so
 * the map controls are unchanged.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MapLayersControl } from '@/components/map/MapLayersControl';

const baseProps = {
  showHeatmap: false,
  onToggleHeatmap: () => {},
  showCoverage: false,
  onToggleCoverage: () => {},
  sidebarOpen: false,
  onToggleSidebar: () => {},
  variant: 'inline' as const,
};

describe('MapLayersControl — VDL2 ACARS toggle', () => {
  it('renders the ACARS pill when onToggleVdl2 is provided', () => {
    const onToggleVdl2 = vi.fn();
    render(<MapLayersControl {...baseProps} showVdl2={false} onToggleVdl2={onToggleVdl2} />);
    const pill = screen.getByTestId('map-toggle-vdl2');
    expect(pill.textContent).toContain('ACARS');
    expect(pill.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(pill);
    expect(onToggleVdl2).toHaveBeenCalledTimes(1);
  });

  it('reflects the active state via aria-pressed', () => {
    render(<MapLayersControl {...baseProps} showVdl2 onToggleVdl2={() => {}} />);
    expect(screen.getByTestId('map-toggle-vdl2').getAttribute('aria-pressed')).toBe('true');
  });

  it('omits the ACARS pill entirely when onToggleVdl2 is absent (VDL2 off)', () => {
    render(<MapLayersControl {...baseProps} />);
    expect(screen.queryByTestId('map-toggle-vdl2')).toBeNull();
    // The other layer pills still render.
    expect(screen.getByTestId('map-toggle-heatmap')).toBeTruthy();
    expect(screen.getByTestId('map-toggle-coverage')).toBeTruthy();
  });
});
