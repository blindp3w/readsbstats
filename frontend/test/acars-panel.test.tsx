/**
 * AcarsPanel error-branch coverage (audit 2026-06-20 gap). The "Failed to load
 * ACARS" Alert had no assertion — a regression swallowing it would silently hide
 * ACARS load failures.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('@/hooks/useVdl2Enabled', () => ({ useVdl2FlightMessages: vi.fn() }));

import { AcarsPanel } from '@/components/vdl2/AcarsPanel';
import { useVdl2FlightMessages } from '@/hooks/useVdl2Enabled';

const mockHook = vi.mocked(useVdl2FlightMessages);

describe('AcarsPanel', () => {
  it('shows the error Alert when the messages query errors', () => {
    mockHook.mockReturnValue({
      available: true, isLoading: false, isSuccess: false, isError: true,
      error: new Error('boom'), messages: [], hasMore: false,
    } as never);
    render(<AcarsPanel icao="48e95d" firstSeen={1000} lastSeen={2000} />);
    expect(screen.getByTestId('flight-acars-card'))
      .toHaveTextContent('Failed to load ACARS: boom');
  });

  it('renders nothing when VDL2 is unavailable', () => {
    mockHook.mockReturnValue({
      available: false, isLoading: false, isSuccess: false, isError: false,
      error: null, messages: [], hasMore: false,
    } as never);
    const { container } = render(
      <AcarsPanel icao="48e95d" firstSeen={1000} lastSeen={2000} />);
    expect(container).toBeEmptyDOMElement();
  });
});
