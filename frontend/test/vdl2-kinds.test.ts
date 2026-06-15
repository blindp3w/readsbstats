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

  it('has no key that is a prefix of another (first-match is unambiguous)', () => {
    const keys = Object.keys(VDL2_BODY_KINDS);
    for (const a of keys)
      for (const b of keys)
        if (a !== b) expect(b.startsWith(a)).toBe(false);
  });
});
