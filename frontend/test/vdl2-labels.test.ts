/**
 * ACARS label dictionary (lib/vdl2Labels.ts) — human-readable names for the
 * 2-char label codes. `labelName` returns null for unknown codes (callers fall
 * back to the bare code), and the dictionary must cover every label observed
 * in production (6.4-day live dump, 2026-06) so badges never show a bare code
 * for routine traffic.
 */
import { describe, it, expect } from 'vitest';
import { labelName, VDL2_LABEL_NAMES } from '@/lib/vdl2Labels';

// Every non-empty label observed in the production dump (69 codes).
const PRODUCTION_LABELS = [
  'H1', '_D', 'Q0', '16', '17', 'B9', 'SA', '49', 'QQ', '8C', '8A', '5U',
  '1L', '83', '8F', 'QR', '36', 'QP', 'QS', '38', '80', '8S', '26', 'B6',
  '10', '37', 'H2', 'Q5', 'CD', 'HX', '4W', '27', '30', '22', 'QX', '18',
  'B0', 'MA', '12', '1B', '20', '2F', '33', '3J', '44', '84', '2T', '88',
  '8B', 'B3', '13', '14', '1M', '2A', '2P', '34', '35', '82', 'CA', 'Q3',
  'VK', '11', '15', '19', '2Z', '3P', '42', '85', 'Q1', 'Q6',
];

describe('labelName', () => {
  it('returns the human name for a known code', () => {
    expect(labelName('Q0')).toBe('Link test');
    expect(labelName('QP')).toContain('OUT report');
    expect(labelName('QQ')).toContain('OFF report');
    expect(labelName('QR')).toContain('ON report');
    expect(labelName('QS')).toContain('IN report');
  });

  it('returns null for unknown, null and empty codes', () => {
    expect(labelName('ZZ')).toBeNull();
    expect(labelName(null)).toBeNull();
    expect(labelName(undefined)).toBeNull();
    expect(labelName('')).toBeNull();
  });

  it('normalizes case and whitespace before lookup', () => {
    expect(labelName(' h1 ')).toBe(labelName('H1'));
    expect(labelName('q0')).toBe('Link test');
  });

  it('covers every label observed in production', () => {
    const missing = PRODUCTION_LABELS.filter((c) => labelName(c) === null);
    expect(missing).toEqual([]);
  });

  it('names airline-defined labels honestly instead of inventing semantics', () => {
    // Label 49 is airline-defined (NOT ARINC-standard) — the name must say so.
    expect(VDL2_LABEL_NAMES['49']).toMatch(/airline/i);
  });
});
