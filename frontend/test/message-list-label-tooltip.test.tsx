/**
 * MessageList label badges — known label codes get a SimpleTooltip with the
 * human-readable name (revealed on focus, the deterministic jsdom path);
 * unknown codes render a bare badge with no tooltip.
 */
import { describe, it, expect } from 'vitest';
import { render, fireEvent, waitFor, screen } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MessageList } from '@/components/vdl2/MessageList';
import type { Vdl2Message } from '@/lib/types';

function msg(over: Partial<Vdl2Message>): Vdl2Message {
  return {
    id: 1,
    ts: 1781000000,
    icao_hex: '4d2228',
    registration: null,
    flight: null,
    label: null,
    dsta: null,
    body: null,
    ...over,
  } as Vdl2Message;
}

function renderList(messages: Vdl2Message[]) {
  return render(
    <TooltipProvider delayDuration={0}>
      <MessageList messages={messages} />
    </TooltipProvider>,
  );
}

describe('MessageList label tooltips', () => {
  it('known label badge reveals its name on focus', async () => {
    renderList([msg({ id: 1, label: 'Q0' })]);
    const badge = screen.getByText('Q0');
    badge.focus();
    fireEvent.focus(badge);
    await waitFor(() => {
      // Radix portals the content into document.body.
      expect(document.body.textContent).toContain('Link test');
    });
  });

  it('unknown label renders a bare badge with no tooltip content', async () => {
    renderList([msg({ id: 1, label: 'ZZ' })]);
    const badge = screen.getByText('ZZ');
    fireEvent.focus(badge);
    // Bare badge: not focusable as a tooltip trigger, nothing portalled.
    expect(document.body.textContent).toContain('ZZ');
    expect(badge.getAttribute('tabindex')).toBeNull();
  });
});

describe('MessageList a11y and wrapping (batch 3)', () => {
  it('hex and registration filter buttons have accessible names', () => {
    render(
      <TooltipProvider delayDuration={0}>
        <MessageList
          messages={[msg({ id: 1, icao_hex: 'abc123', registration: 'SP-LYF' })]}
          onHexClick={() => {}}
          onRegClick={() => {}}
        />
      </TooltipProvider>,
    );
    expect(screen.getByRole('button', { name: /filter by hex abc123/i })).toBeTruthy();
    expect(screen.getByRole('button', { name: /filter by registration SP-LYF/i })).toBeTruthy();
  });

  it('the partial badge has a text equivalent that works without hover', () => {
    renderList([msg({ id: 1, more: 1 })]);
    const badge = screen.getByTestId('vdl2-row-partial');
    expect(badge.textContent).toMatch(/partial/);
    expect(badge.textContent).toMatch(/intermediate block/i);
  });

  it('long single-token decoded values wrap instead of widening the row', () => {
    const long = 'X'.repeat(200);
    render(
      <TooltipProvider delayDuration={0}>
        <MessageList
          messages={[msg({ id: 1, body: long })]}
          decode={() => ({ description: 'D', items: [{ label: 'Body', value: long }] })}
        />
      </TooltipProvider>,
    );
    const chip = screen.getByTestId('vdl2-decoded').firstElementChild as HTMLElement;
    expect(chip.className).toMatch(/break-all/);
    expect(chip.className).toMatch(/min-w-0/);
    expect(chip.className).toMatch(/whitespace-pre-wrap/);
    expect(screen.getByTestId('vdl2-decoded').className).toMatch(/min-w-0/);
    expect(screen.getByTestId('vdl2-message-row').className).toMatch(/min-w-0/);
  });
});
