/**
 * Audit 17: MessageList renders untrusted upstream ACARS `body` text. It must
 * render as escaped React children (inside <pre>{body}</pre>), never as raw
 * HTML — this test locks the XSS-safe contract the component comments promise.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MessageList } from '@/components/vdl2/MessageList';
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
