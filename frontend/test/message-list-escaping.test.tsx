/**
 * Audit 17: MessageList renders untrusted upstream ACARS `body` text. It must
 * render as escaped React children (inside <pre>{body}</pre>), never as raw
 * HTML — this test locks the XSS-safe contract the component comments promise.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { MessageList } from '@/components/vdl2/MessageList';
import { useClockStore } from '@/store/clockFormat';
import type { Vdl2Message } from '@/lib/types';

describe('MessageList body rendering (XSS-safe contract)', () => {
  it('renders an HTML-bearing ACARS body as escaped text, not injected DOM', () => {
    const malicious = '<img src=x onerror=alert(1)><b>bold</b>';
    const msg = { id: 1, ts: 1_749_000_000, body: malicious } as Vdl2Message;
    const { container } = render(<MessageList messages={[msg]} />);
    // The body appears verbatim as text...
    expect(screen.getByText(malicious)).toBeTruthy();
    // ...and no real <img>/<b> elements were created from it.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });
});

// BUG-8: timestamps must use the reactive useFormat() hook, not a bare one-shot
// import of fmtTs — otherwise toggling the 12h/24h clock-format store leaves the
// rendered timestamp stale until a full remount.
describe('MessageList timestamp reactivity (BUG-8)', () => {
  beforeEach(() => {
    localStorage.clear();
    useClockStore.setState({ clockFormat: '24h' });
  });
  afterEach(() => {
    localStorage.clear();
    useClockStore.setState({ clockFormat: '24h' });
  });

  it('re-renders the timestamp when the clock-format store toggles', () => {
    const msg = { id: 1, ts: 1_749_000_000, body: 'hi' } as Vdl2Message;
    render(<MessageList messages={[msg]} />);

    const row = screen.getByTestId('vdl2-message-row');
    const before = row.querySelector('.tabnum')!.textContent ?? '';
    // 24h must not carry an AM/PM marker.
    expect(before).not.toMatch(/\b(AM|PM)\b/);

    act(() => {
      useClockStore.getState().setClockFormat('12h');
    });

    const after = row.querySelector('.tabnum')!.textContent ?? '';
    expect(after).not.toBe(before);
    expect(after).toMatch(/\b(AM|PM)\b/);
  });
});
