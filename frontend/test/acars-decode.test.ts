import { describe, it, expect } from 'vitest';
import { MessageDecoder } from '@airframes/acars-decoder';
import { decodeAcars } from '@/lib/acarsDecode';
import type { Vdl2Message } from '@/lib/types';

const dec = new MessageDecoder();
const mk = (label: string | null, body: string | null): Vdl2Message =>
  ({ id: 1, ts: 0, label, body } as Vdl2Message);

describe('decodeAcars', () => {
  it('decodes a QR OOOI body into description + items', () => {
    const d = decodeAcars(mk('QR', 'LIMCEPMO1009'), dec);
    expect(d).not.toBeNull();
    expect(d!.description).toBe('ON Report');
    const origin = d!.items.find((i) => i.label === 'Origin');
    expect(origin?.value).toBe('LIMC');
  });

  it('returns null for an undecodable #DFB body', () => {
    expect(decodeAcars(mk('H1', '#DFBABS011DA_S       UAAAEPWA2'), dec)).toBeNull();
  });

  it('returns null when body or label is missing', () => {
    expect(decodeAcars(mk('H1', ''), dec)).toBeNull();
    expect(decodeAcars(mk(null, 'LIMCEPMO1009'), dec)).toBeNull();
  });

  it('extracts the remaining (undecoded tail) when the decoder leaves one', () => {
    // Real Label-16 AUTPOS body: decoder parses position/altitude/ETA and leaves a tail.
    const d = decodeAcars(mk('16', '200355,8713,2016,  88,N 52.085 E 20.654'), dec);
    expect(d).not.toBeNull();
    expect(d!.remaining).toBeDefined();
    expect(d!.remaining).toContain('88');
  });

  it('returns null when the decoder throws', () => {
    const throwing = { decode: () => { throw new Error('boom'); } } as unknown as typeof dec;
    expect(decodeAcars(mk('QR', 'LIMCEPMO1009'), throwing)).toBeNull();
  });
});
