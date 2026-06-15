import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MessageList } from '@/components/vdl2/MessageList';
import type { Vdl2Message } from '@/lib/types';
import type { DecodedAcars } from '@/lib/acarsDecode';

function renderList(msg: Vdl2Message, decode?: (m: Vdl2Message) => DecodedAcars | null) {
  render(
    <TooltipProvider delayDuration={0}>
      <MessageList messages={[msg]} decode={decode} />
    </TooltipProvider>,
  );
}

const acmsRow = {
  id: 1, ts: 1_749_000_000, label: 'H1', body: '#DFBABS011DA_S UAAAEPWA2',
} as Vdl2Message;

describe('MessageList body-kind chip', () => {
  it('shows a category chip for a known body prefix', () => {
    renderList(acmsRow);
    expect(screen.getByTestId('vdl2-kind').textContent).toBe('ACMS report');
  });

  it('shows no chip for an unrecognized body', () => {
    renderList({ id: 2, ts: 1_749_000_000, label: 'Q0', body: 'free text' } as Vdl2Message);
    expect(screen.queryByTestId('vdl2-kind')).toBeNull();
  });

  it('suppresses the chip when the airframes decoder produced a result', () => {
    const decode = () => ({ description: 'Decoded', items: [], remaining: '' }) as DecodedAcars;
    renderList(acmsRow, decode);
    expect(screen.queryByTestId('vdl2-kind')).toBeNull();
    expect(screen.getByTestId('vdl2-decoded-desc')).toBeTruthy();
  });

  it('suppresses the chip when the row already has a filed_route line', () => {
    renderList({
      id: 3, ts: 1_749_000_000, label: 'H1',
      body: '#M1BPOSN52086E019235...',
      filed_route: { dep: 'EPWA', arr: 'EHAM' },
    } as Vdl2Message);
    expect(screen.queryByTestId('vdl2-kind')).toBeNull();
    expect(screen.getByTestId('vdl2-filed-route')).toBeTruthy();
  });
});
