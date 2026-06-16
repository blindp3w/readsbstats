import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { KpiCard } from '@/components/stats/KpiCard';

function renderKpi(ui: React.ReactNode) {
  return render(<TooltipProvider delayDuration={0}>{ui}</TooltipProvider>);
}

describe('KpiCard', () => {
  it('renders an up-delta with +pct when prev > 0 and value > prev', () => {
    renderKpi(<KpiCard label="Flights" value={120} prev={100} testid="kpi-flights" />);
    const card = screen.getByTestId('kpi-flights');
    expect(card.textContent).toContain('+20');
    expect(card.textContent).toContain('(+20%)');
  });

  it('renders a down-delta in danger color with negative pct', () => {
    renderKpi(<KpiCard label="Flights" value={80} prev={100} testid="kpi-flights" />);
    const card = screen.getByTestId('kpi-flights');
    expect(card.textContent).toContain('20');
    // Negative pct uses the U+2212 minus sign (matching the delta value's
    // glyph), not the ASCII hyphen-minus, so the two signs render consistently.
    expect(card.textContent).toContain('(−20%)');
  });

  it('renders an em-dash when prev is null', () => {
    renderKpi(<KpiCard label="Flights" value={120} prev={null} testid="kpi-flights" />);
    const card = screen.getByTestId('kpi-flights');
    expect(card.textContent).toContain('—');
  });

  it('renders an em-dash when prev is undefined', () => {
    renderKpi(<KpiCard label="Flights" value={120} testid="kpi-flights" />);
    expect(screen.getByTestId('kpi-flights').textContent).toContain('—');
  });

  it('renders a string value verbatim (e.g., formatted distance)', () => {
    renderKpi(<KpiCard label="Max range" value="312 nm" testid="kpi-range" />);
    expect(screen.getByTestId('kpi-range').textContent).toContain('312 nm');
  });

  it('hides the sparkline below MIN_POINTS (<7) but shows it above', () => {
    const { rerender } = render(
      <TooltipProvider delayDuration={0}>
        <KpiCard label="Flights" value={1} series={[1, 2, 3]} testid="kpi" />
      </TooltipProvider>,
    );
    expect(screen.queryByTestId('kpi-sparkline')).toBeNull();
    rerender(
      <TooltipProvider delayDuration={0}>
        <KpiCard label="Flights" value={1} series={[1, 2, 3, 4, 5, 6, 7]} testid="kpi" />
      </TooltipProvider>,
    );
    expect(screen.queryByTestId('kpi-sparkline')).not.toBeNull();
  });
});
