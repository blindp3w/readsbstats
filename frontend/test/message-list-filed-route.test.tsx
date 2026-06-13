import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MessageList } from '@/components/vdl2/MessageList';
import type { Vdl2Message } from '@/lib/types';

describe('MessageList filed_route line', () => {
  it('renders a Filed route line from server-parsed #M1BPOS data', () => {
    const msg = {
      id: 1,
      ts: 1_749_000_000,
      label: 'H1',
      body: '#M1BPOSN52086E019235...',
      filed_route: { dep: 'EPWA', arr: 'EHAM', star: 'NORK2A', approach: 'ILS 27.ARTIP' },
    } as Vdl2Message;
    render(
      <TooltipProvider delayDuration={0}>
        <MessageList messages={[msg]} />
      </TooltipProvider>,
    );
    const line = screen.getByTestId('vdl2-filed-route');
    expect(line.textContent).toContain('EPWA');
    expect(line.textContent).toContain('EHAM');
    expect(line.textContent).toContain('NORK2A');
    expect(line.textContent).toContain('ILS 27.ARTIP');
  });

  it('renders no Filed route line when filed_route is absent', () => {
    const msg = { id: 2, ts: 1_749_000_000, label: 'Q0', body: 'clearance' } as Vdl2Message;
    render(
      <TooltipProvider delayDuration={0}>
        <MessageList messages={[msg]} />
      </TooltipProvider>,
    );
    expect(screen.queryByTestId('vdl2-filed-route')).toBeNull();
  });
});
