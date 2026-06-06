import { describe, it, expect } from 'vitest';
import { safeUrl } from '@/lib/safeUrl';

describe('safeUrl — frontend SSRF / XSS allowlist', () => {
  it('accepts https:// URLs', () => {
    expect(safeUrl('https://example.com/photo.jpg')).toBe('https://example.com/photo.jpg');
  });

  it.each([
    'http://example.com',
    'javascript:alert(1)',
    'data:image/png;base64,AAA',
    'vbscript:msgbox',
    'file:///etc/passwd',
    '//evil.com/x.png',
    // SEC-4 (audit 18): credentialed URLs leak userinfo / are a host-
    // confusion vector — reject even over https.
    'https://user:pass@evil.com/p.jpg',
    'https://user@evil.com/p.jpg',
  ])('rejects %s', (input) => {
    expect(safeUrl(input)).toBe('');
  });

  it.each([null, undefined, '', '   '])('rejects empty input %s', (input) => {
    expect(safeUrl(input)).toBe('');
  });

  it('rejects malformed URLs', () => {
    expect(safeUrl('not a url')).toBe('');
  });

  it('trims whitespace before validating', () => {
    expect(safeUrl('  https://x.com  ')).toBe('https://x.com');
  });
});
