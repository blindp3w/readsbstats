/**
 * AboutReceiverFooter coverage (audit 2026-06-20 gap). The em-dash fallbacks and
 * the source-breakdown string were only exercised indirectly.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AboutReceiverFooter } from '@/components/stats/AboutReceiverFooter';

describe('AboutReceiverFooter', () => {
  it('falls back to em-dash when optional values are absent', () => {
    render(<AboutReceiverFooter totalFlights={5} />);
    // DB size (null) and Sources (undefined) both render "—".
    expect(screen.getByTestId('stats-about-rows')).toHaveTextContent('—');
  });

  it('renders the source breakdown when provided', () => {
    render(
      <AboutReceiverFooter
        totalFlights={5}
        dbSizeBytes={1024}
        sourceBreakdown={{ adsb: 60, mlat: 30, other: 10 }}
      />,
    );
    expect(screen.getByTestId('stats-about-rows'))
      .toHaveTextContent('60% ADS-B · 30% MLAT · 10% other');
  });
});
