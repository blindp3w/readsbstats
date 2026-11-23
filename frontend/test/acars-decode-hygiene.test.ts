import { describe, it, expect } from 'vitest';
import type { MessageDecoder } from '@airframes/acars-decoder';
import { decodeAcars } from '@/lib/acarsDecode';
import type { Vdl2Message } from '@/lib/types';

const mk = (label: string | null, body: string | null, extra: Partial<Vdl2Message> = {}): Vdl2Message =>
  ({ id: 1, ts: 0, label, body, ...extra }) as Vdl2Message;

type Item = { type: string; code: string; label: string; value: string };
// Stub decoder returning a canned airframes-shaped result; records the Message it was given.
function stub(items: Item[], opts: { level?: string; raw?: Record<string, unknown> } = {}) {
  const seen: unknown[] = [];
  const dec = {
    decode: (m: unknown) => {
      seen.push(m);
      return {
        decoded: true,
        decoder: { name: 'stub', type: 'pattern-match', decodeLevel: opts.level ?? 'partial' },
        formatted: { description: 'CPDLC Message', items },
        raw: opts.raw ?? {},
        remaining: {},
      };
    },
  } as unknown as MessageDecoder;
  return { dec, seen };
}

describe('decodeAcars hygiene — echoed body', () => {
  it('returns null when the only item repeats the body minus its leading "/"', () => {
    const { dec } = stub([{ type: 'text', code: 'TEXT', label: 'Text Message', value: 'ABC DEF' }]);
    expect(decodeAcars(mk('B6', '/ABC DEF'), dec)).toBeNull();
  });
  it('returns null when the only item repeats the whole body (after trim)', () => {
    const { dec } = stub([{ type: 'text', code: 'TEXT', label: 'Text', value: 'hello' }]);
    expect(decodeAcars(mk('37', 'hello \r\n'), dec)).toBeNull();
  });
  it('drops echo items but keeps the informative ones', () => {
    const { dec } = stub([
      { type: 'text', code: 'TEXT', label: 'Text', value: 'ABC' },
      { type: 'icao', code: 'ORG', label: 'Origin', value: 'EPWA' },
    ]);
    const d = decodeAcars(mk('40', '/ABC'), dec);
    expect(d?.items).toEqual([expect.objectContaining({ label: 'Origin', value: 'EPWA' })]);
  });
  it('returns null for a description with no items at all', () => {
    const { dec } = stub([]);
    expect(decodeAcars(mk('B6', '/x'), dec)).toBeNull();
  });
  it('real library: B6 "/…" body is no longer reported as decoded', async () => {
    const { MessageDecoder } = await import('@airframes/acars-decoder');
    expect(decodeAcars(mk('B6', '/CPDLC something'), new MessageDecoder())).toBeNull();
  });
});

describe('decodeAcars — extended optional fields', () => {
  it('exposes item type/code, raw and decode level', () => {
    const { dec } = stub(
      [{ type: 'icao', code: 'ORG', label: 'Origin', value: 'LIMC' }],
      { level: 'full', raw: { departure_icao: 'LIMC' } },
    );
    const d = decodeAcars(mk('QR', 'LIMCEPMO1009'), dec)!;
    expect(d.items[0]).toEqual({ label: 'Origin', value: 'LIMC', type: 'icao', code: 'ORG' });
    expect(d.raw).toEqual({ departure_icao: 'LIMC' });
    expect(d.level).toBe('full');
  });
  it('omits level when the decoder reports an unknown value', () => {
    const { dec } = stub([{ type: 'a', code: 'B', label: 'L', value: 'v' }], { level: 'weird' });
    expect(decodeAcars(mk('QR', 'LIMCEPMO1009'), dec)?.level).toBeUndefined();
  });
});

describe('decodeAcars — sublabel', () => {
  it('passes sublabel through but keeps the text unprefixed', () => {
    const { dec, seen } = stub([{ type: 'a', code: 'B', label: 'L', value: 'v' }]);
    decodeAcars(mk('H1', 'POSN52', { sublabel: 'M1' }), dec);
    expect(seen[0]).toEqual({ label: 'H1', text: 'POSN52', sublabel: 'M1' });
  });
  it('omits sublabel when the message has none', () => {
    const { dec, seen } = stub([{ type: 'a', code: 'B', label: 'L', value: 'v' }]);
    decodeAcars(mk('H1', 'POSN52', { sublabel: null }), dec);
    expect(seen[0]).toEqual({ label: 'H1', text: 'POSN52' });
    expect('sublabel' in (seen[0] as object)).toBe(false);
  });
});
