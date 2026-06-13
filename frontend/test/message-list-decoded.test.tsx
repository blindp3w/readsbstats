import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MessageDecoder } from '@airframes/acars-decoder';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MessageList } from '@/components/vdl2/MessageList';
import { decodeAcars } from '@/lib/acarsDecode';
import type { Vdl2Message } from '@/lib/types';

const dec = new MessageDecoder();
const decode = (m: Vdl2Message) => decodeAcars(m, dec);

describe('MessageList decoded rendering (Layout A)', () => {
  it('shows decoded description + chips above the raw body', () => {
    const msg = { id: 1, ts: 1_749_000_000, label: 'QR', body: 'LIMCEPMO1009' } as Vdl2Message;
    render(
      <TooltipProvider delayDuration={0}>
        <MessageList messages={[msg]} decode={decode} />
      </TooltipProvider>,
    );
    expect(screen.getByText('ON Report')).toBeTruthy();          // description
    expect(screen.getByText('LIMC')).toBeTruthy();               // chip value
    expect(screen.getByText('LIMCEPMO1009')).toBeTruthy();       // raw still present
  });

  it('shows raw only for an undecoded body', () => {
    const msg = { id: 2, ts: 1_749_000_000, label: 'H1', body: '#DFBABS' } as Vdl2Message;
    render(
      <TooltipProvider delayDuration={0}>
        <MessageList messages={[msg]} decode={decode} />
      </TooltipProvider>,
    );
    expect(screen.queryByTestId('vdl2-decoded')).toBeNull();
    expect(screen.getByText('#DFBABS')).toBeTruthy();
  });
});
