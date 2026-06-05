/**
 * Regression for FE-001: every MapLayersControl call site in MapCommandBar
 * (inline-lg / popover-md-sm / mobile-expanded) must receive the VDL2 toggle
 * props. The mobile site was missing them. We mock MapLayersControl to capture
 * `onToggleVdl2` per instance — this catches a per-site omission directly,
 * without fighting Radix popovers / responsive `hidden` classes in jsdom.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MapCommandBar } from '@/components/map/MapCommandBar';

vi.mock('@/components/map/MapLayersControl', () => ({
  MapLayersControl: (props: { variant: string; onToggleVdl2?: () => void }) => (
    <div
      data-testid="mlc"
      data-variant={props.variant}
      data-has-vdl2={props.onToggleVdl2 ? '1' : '0'}
    />
  ),
}));

function baseProps(overrides: Record<string, unknown> = {}) {
  const noop = () => {};
  return {
    mode: 'live' as const,
    onModeChange: noop,
    mapWindow: '24h' as const,
    onMapWindowChange: noop,
    showHeatmap: false,
    onToggleHeatmap: noop,
    showCoverage: false,
    onToggleCoverage: noop,
    sidebarOpen: false,
    onToggleSidebar: noop,
    snapshotAt: null,
    snapshotIsError: false,
    snapshotIsStale: false,
    aircraftCount: 0,
    scrubMin: 0,
    scrubMax: 100,
    scrubValue: 100,
    onScrubChange: noop,
    onSeek: noop,
    onJumpNow: noop,
    rewindLabel: '',
    playing: false,
    onPlayToggle: noop,
    speed: 1 as const,
    onSpeedChange: noop,
    histDateISO: '2026-06-05',
    histTimeHHMM: '12:00',
    onHistDateChange: noop,
    onHistTimeChange: noop,
    histMinSec: 0,
    histMaxSec: 100,
    ...overrides,
  };
}

function renderBar(overrides: Record<string, unknown> = {}) {
  return render(
    <TooltipProvider delayDuration={0}>
      <MapCommandBar {...(baseProps(overrides) as React.ComponentProps<typeof MapCommandBar>)} />
    </TooltipProvider>,
  );
}

describe('MapCommandBar — VDL2 props reach every layer-control site', () => {
  it('passes onToggleVdl2 to ALL MapLayersControl instances when provided', () => {
    renderBar({ showVdl2: false, onToggleVdl2: () => {}, vdl2Loading: false });
    // Expand the mobile bar so its (previously-missing) layer control mounts.
    fireEvent.click(screen.getByTestId('map-mobile-expand'));
    const instances = screen.getAllByTestId('mlc');
    expect(instances.length).toBeGreaterThanOrEqual(3); // inline + popover + mobile
    // The mobile site previously omitted the props — every site must have them now.
    for (const el of instances) {
      expect(el.getAttribute('data-has-vdl2')).toBe('1');
    }
  });

  it('omits onToggleVdl2 everywhere when the feature is unavailable', () => {
    renderBar({ showVdl2: false, onToggleVdl2: undefined, vdl2Loading: false });
    fireEvent.click(screen.getByTestId('map-mobile-expand'));
    for (const el of screen.getAllByTestId('mlc')) {
      expect(el.getAttribute('data-has-vdl2')).toBe('0');
    }
  });
});
