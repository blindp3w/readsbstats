/**
 * IsolationPills interaction test (Audit 2026-06-20 coverage gap). The pill row
 * is reused by Flight / Metrics / History for series isolation but had no direct
 * test — an off-by-one in the keys/labels/colors index mapping or a broken toggle
 * would have shipped silently.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, screen } from '@testing-library/react';
import { IsolationPills } from '@/components/charts/IsolationPills';

const KEYS = ['ac_adsb', 'ac_mlat'];
const LABELS = ['ADS-B', 'MLAT'];
const COLORS = ['#00ff00', '#ffff00'];

describe('IsolationPills', () => {
  it('clicking an inactive pill isolates that series', () => {
    const onChange = vi.fn();
    render(
      <IsolationPills keys={KEYS} labels={LABELS} colors={COLORS}
        isolated={null} onChange={onChange} testIdPrefix="t" />,
    );
    fireEvent.click(screen.getByTestId('t-pill-ac_adsb'));
    expect(onChange).toHaveBeenCalledWith('ac_adsb');
  });

  it('clicking the active pill clears isolation; aria-pressed reflects state', () => {
    const onChange = vi.fn();
    render(
      <IsolationPills keys={KEYS} labels={LABELS} colors={COLORS}
        isolated="ac_adsb" onChange={onChange} testIdPrefix="t" />,
    );
    expect(screen.getByTestId('t-pill-ac_adsb')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('t-pill-ac_mlat')).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByTestId('t-pill-ac_adsb'));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it('falls back to the key when labels are shorter than keys', () => {
    const onChange = vi.fn();
    render(
      <IsolationPills keys={['solo']} labels={[]} colors={[]}
        isolated={null} onChange={onChange} testIdPrefix="t" />,
    );
    const pill = screen.getByTestId('t-pill-solo');
    expect(pill).toHaveTextContent('solo');
    expect(pill).toHaveAttribute('aria-label', 'Isolate solo');
  });
});
