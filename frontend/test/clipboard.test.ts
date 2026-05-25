import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { copyToClipboard } from '@/lib/clipboard';

// The production deploy serves over plain HTTP, so `navigator.clipboard`
// is undefined on the Pi and `execCommand('copy')` is the real path.
// Both branches are tested here.

describe('copyToClipboard', () => {
  const originalExecCommand = document.execCommand;
  const originalIsSecureContext = Object.getOwnPropertyDescriptor(
    window,
    'isSecureContext',
  );

  beforeEach(() => {
    document.execCommand = vi.fn(() => true);
  });

  afterEach(() => {
    document.execCommand = originalExecCommand;
    if (originalIsSecureContext) {
      Object.defineProperty(window, 'isSecureContext', originalIsSecureContext);
    }
    // Drop any test-set navigator.clipboard so tests don't leak.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (navigator as any).clipboard;
  });

  it('uses navigator.clipboard.writeText in a secure context', async () => {
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      value: true,
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const ok = await copyToClipboard('RSBS_MAX_RANGE');
    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalledWith('RSBS_MAX_RANGE');
    expect(document.execCommand).not.toHaveBeenCalled();
  });

  it('falls back to document.execCommand in a non-secure context', async () => {
    // Production path: plain HTTP, navigator.clipboard undefined.
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      value: false,
    });
    const ok = await copyToClipboard('RSBS_TELEGRAM_TOKEN');
    expect(ok).toBe(true);
    expect(document.execCommand).toHaveBeenCalledWith('copy');
  });

  it('falls back to execCommand when navigator.clipboard.writeText rejects', async () => {
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      value: true,
    });
    const writeText = vi.fn().mockRejectedValue(new Error('blocked'));
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    const ok = await copyToClipboard('RSBS_POLL_INTERVAL');
    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalled();
    expect(document.execCommand).toHaveBeenCalledWith('copy');
  });

  it('returns false when execCommand fails', async () => {
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      value: false,
    });
    document.execCommand = vi.fn(() => false);
    const ok = await copyToClipboard('RSBS_X');
    expect(ok).toBe(false);
  });
});
