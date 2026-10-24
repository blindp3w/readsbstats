/**
 * Body-kind dictionary (lib/vdl2Kinds.ts) — heuristic human categories for ACARS
 * message bodies, keyed by body prefix. `bodyKind` returns null for unknown/empty
 * input (caller renders no chip). Names are display-only and fail-soft.
 */
import { describe, it, expect } from 'vitest';
import { bodyKind, VDL2_BODY_KINDS } from '@/lib/vdl2Kinds';

describe('bodyKind', () => {
  it('categorizes known body prefixes', () => {
    expect(bodyKind('#DFBABS011DA_S UAAAEPWA2')).toBe('ACMS report');
    expect(bodyKind('#CFBWRN/PNRC... FAULT')).toBe('Maintenance (CMS)');
    expect(bodyKind('#T8BR642V0110,170302,...')).toBe('Engine report');
    expect(bodyKind('#T1BCKFk4f3x...')).toBe('AID report');
    expect(bodyKind('#EIBRPT12;PG1;REAL')).toBe('Brake/system report');
    expect(bodyKind('01ICCL     LOT71/051422EPWAVIDP')).toBe('Performance report');
    expect(bodyKind('OHMAeJydU01v...')).toBe('Boeing OHMA');
  });

  it('returns null for unknown, null and empty bodies', () => {
    expect(bodyKind('just some free text')).toBeNull();
    expect(bodyKind('')).toBeNull();
    expect(bodyKind(null)).toBeNull();
    expect(bodyKind(undefined)).toBeNull();
  });

  it('disambiguates 59,G by label (36 = airborne position, 37 = ground/airport)', () => {
    expect(bodyKind('59,G,0542,1,1,EPWA,52.15,20.59', '36')).toBe('Position report');
    expect(bodyKind('59,G,EPGD,EPWA,33/-', '37')).toBe('Ground report');
    expect(bodyKind('59,G,1')).toBe('Position report'); // unknown label → default position
    expect(bodyKind('59,X,whatever')).toBeNull();
  });

  describe('dumpvdl2 sublabel (H1 prefix stripped from body)', () => {
    it('rebuilds the downlink prefix from the sublabel', () => {
      expect(bodyKind('POSN52081E020017,...', 'H1', 'M1')).toBe('FMS position/route');
      expect(bodyKind('ABS011DA_S UAAAEPWA2', 'H1', 'DF')).toBe('ACMS report');
      expect(bodyKind('WRN/PNRC... FAULT', 'H1', 'CF')).toBe('Maintenance (CMS)');
      expect(bodyKind('CKFk4f3x...', 'H1', 'T1')).toBe('AID report');
    });

    it('keeps OHMA as Boeing OHMA even though its sublabel is T1', () => {
      expect(bodyKind('OHMAeJydU01v...', 'H1', 'T1')).toBe('Boeing OHMA');
    });

    it('returns null for an unknown sublabel with an unrecognized body', () => {
      expect(bodyKind('free text', 'H1', 'ZZ')).toBeNull();
    });

    it('behaves as before when the sublabel is null, undefined or empty', () => {
      for (const sub of [null, undefined, '']) {
        expect(bodyKind('POSN52081E020017', 'H1', sub)).toBeNull();
        expect(bodyKind('#DFBABS011DA_S UAAAEPWA2', 'H1', sub)).toBe('ACMS report');
        expect(bodyKind('OHMAeJydU01v...', 'H1', sub)).toBe('Boeing OHMA');
      }
    });

    it('still matches vdlm2dec-style prefixed bodies regardless of sublabel', () => {
      expect(bodyKind('#DFBABS011DA_S UAAAEPWA2', 'H1', 'M1')).toBe('ACMS report');
      expect(bodyKind('#M1BPOSN52086E019235', 'H1', null)).toBe('FMS position/route');
    });

    it('leaves 59,G label handling unchanged with a sublabel present', () => {
      expect(bodyKind('59,G,0542,1,1,EPWA', '36', 'M1')).toBe('Position report');
      expect(bodyKind('59,G,EPGD,EPWA,33/-', '37', 'M1')).toBe('Ground report');
    });
  });

  it('has no key that is a prefix of another (first-match is unambiguous)', () => {
    const keys = Object.keys(VDL2_BODY_KINDS);
    for (const a of keys)
      for (const b of keys)
        if (a !== b) expect(b.startsWith(a)).toBe(false);
  });
});
