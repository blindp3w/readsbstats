/**
 * MessageList correctly *consumes* the @tanstack/react-virtual API: it sizes the
 * <ul> spacer from getTotalSize() and positions each <li> at its virtual item's
 * index/start. The virtualizer is mocked (test/setup.ts) to a deterministic
 * pass-through (start = index*100, total = count*100), so these assertions pin
 * OUR wiring of the API — not react-virtual's own windowing math. A regression
 * that drops the transform, mis-indexes a row, or hard-codes the spacer height
 * fails here even though the content-focused message-list suites stay green.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TooltipProvider } from '@/components/ui/Tooltip';
import { MessageList } from '@/components/vdl2/MessageList';
import type { Vdl2Message } from '@/lib/types';

const msgs: Vdl2Message[] = [
  { id: 10, ts: 1_749_000_000, body: 'first-body' } as Vdl2Message,
  { id: 11, ts: 1_749_000_100, body: 'second-body' } as Vdl2Message,
  { id: 12, ts: 1_749_000_200, body: 'third-body' } as Vdl2Message,
];

function renderList() {
  return render(
    <TooltipProvider delayDuration={0}>
      <MessageList messages={msgs} />
    </TooltipProvider>,
  );
}

describe('MessageList virtualizer wiring', () => {
  it('sizes the list spacer from getTotalSize()', () => {
    renderList();
    // mock getTotalSize() = count * 100
    expect(screen.getByTestId('vdl2-list').style.height).toBe('300px');
  });

  it('positions each row at its virtual item index/start', () => {
    renderList();
    const rows = screen.getAllByTestId('vdl2-message-row');
    expect(rows).toHaveLength(3);
    rows.forEach((li, i) => {
      expect(li.getAttribute('data-index')).toBe(String(i));
      // mock start = index * 100
      expect(li.style.transform).toBe(`translateY(${i * 100}px)`);
    });
  });

  it('maps each slot to messages[index]', () => {
    renderList();
    const rows = screen.getAllByTestId('vdl2-message-row');
    expect(rows[0].textContent).toContain('first-body');
    expect(rows[1].textContent).toContain('second-body');
    expect(rows[2].textContent).toContain('third-body');
  });
});
