// Guard tests against the REAL @airframes/acars-decoder (not mocked anywhere:
// test/setup.ts only mocks the hook). They pin the contract between the library
// and our two decoding layers (ADR-0017):
//  - every format we decode locally (lib/acarsLocal) must still be undecoded by
//    the library — if a future version takes one over, the local decoder is dead
//    code (or worse, shadowed) and this fails so we notice;
//  - formats we rely on airframes for keep decoding.
import { describe, it, expect } from 'vitest';
import { MessageDecoder } from '@airframes/acars-decoder';
import { decodeAcars } from '@/lib/acarsDecode';
import { decodeLocal } from '@/lib/acarsLocal';
import type { Vdl2Message } from '@/lib/types';

const dec = new MessageDecoder();
const mk = (label: string | null, body: string | null, extra: Partial<Vdl2Message> = {}): Vdl2Message =>
  ({ id: 1, ts: 1_749_000_000, label, body, ...extra }) as Vdl2Message;

// CRC-16/GENIBUS (poly 0x1021, init 0xFFFF, xorout 0xFFFF) — one of the two
// checksum algorithms the library's ARINC-702 plugin accepts on H1 bodies.
function withCrc(data: string): string {
  let crc = 0xffff;
  for (const ch of data) {
    crc ^= ch.charCodeAt(0) << 8;
    for (let i = 0; i < 8; i++) crc = crc & 0x8000 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
  }
  return data + ((crc ^ 0xffff) & 0xffff).toString(16).toUpperCase().padStart(4, '0');
}

// The exact bodies used by test/acars-local.test.ts for each local decoder.
const LOCAL_SAMPLES: [string, string][] = [
  ['SA', '0EV123456V'],
  ['SA', '0LV000102VS2/hello'],
  ['B9', '/EPWA.TI2/024EPWAA1234'],
  ['B9', 'TI2/242EHAMD00AF'],
  ['B9', 'TI2/024CGN A2052'],
  ['5U', '01 WXRQ   0267/23 EPWA/EHAM .SP-LYG\n/TYP 1/STA EHAM/STA EPPO/STA EDDB'],
  ['5U', 'SA EPWA,EPWR'],
  ['5U', 'FT EPWA\r'],
  ['5U', 'EPWA EPKK LHBP  YBE-SA'],
  ['8A', '101010,O,EGSS'],
  ['8C', '101010,O,EGSS'],
  ['8F', '123456,C,EPWA'],
  ['8S', '101010,O,EGSS'],
  ['26', '1049EPWAAF1147/18\r1241LFPG27R'],
  ['20', 'RST53A27145652KLM49N  EHAMEPWA'],
];

describe('real library — local decoders still needed', () => {
  it.each(LOCAL_SAMPLES)('label %s %j: airframes leaves it undecoded, local decoder handles it', (label, body) => {
    const m = mk(label, body);
    const raw = dec.decode({ label, text: body });
    expect(raw.decoded).toBe(false);
    expect(decodeAcars(m, dec)).toBeNull();
    expect(decodeLocal(m)).not.toBeNull();
  });
});

describe('real library — bodies that must keep decoding', () => {
  it('QR OOOI', () => {
    expect(decodeAcars(mk('QR', 'LIMCEPMO1009'), dec)?.description).toBe('ON Report');
  });
  it('H1 M1 POS (ARINC-702 with checksum): position + route', () => {
    const body = withCrc('POSN52081E020017,ABESI,085321,370,ASKIL,090000,XIMAL,M52,28049,1070');
    const d = decodeAcars(mk('H1', body, { sublabel: 'M1' }), dec);
    expect(d?.description).toBe('Position Report');
    expect(d?.items.find((i) => i.code === 'POS')?.value).toBe('52.135 N, 20.028 E');
    expect(d?.items.find((i) => i.code === 'ALT')?.value).toBe('37000 feet');
    expect(d?.level).toBe('partial');
  });
  it('H1 PRG (ARINC-702 with checksum)', () => {
    const d = decodeAcars(mk('H1', withCrc('PRG/FNLO0123')), dec);
    expect(d?.description).toBe('Progress Report');
    expect(d?.level).toBe('full');
  });
  it('H1 CF WRN', () => {
    const body = 'WRN/SN/WN241123101500E123456WARNING TEXT HERE';
    const d = decodeAcars(mk('H1', body), dec);
    expect(d?.description).toBe('Warning Message');
    expect(d?.items[0]).toMatchObject({ type: 'warning', code: 'WRN', label: 'Warning Message' });
  });
  it('H1 CF FLR', () => {
    const body = 'FLR/FB1234/FR241123101500E123456FAULT TEXT HERE';
    const d = decodeAcars(mk('H1', body), dec);
    expect(d?.description).toBe('Fault Log Report');
    expect(d?.items[0]).toMatchObject({ type: 'fault', code: 'FR' });
  });
  it('an unprefixed body with sublabel set still decodes (no #M1B re-prefixing needed)', () => {
    const body = withCrc('POSN52081E020017,ABESI,085321,370,ASKIL,090000,XIMAL,M52,28049,1070');
    expect(decodeAcars(mk('H1', body, { sublabel: 'M1' }), dec)).not.toBeNull();
  });
});

describe('real library — undecodable / echo-only stay raw', () => {
  it('#DFB… body → null', () => {
    expect(decodeAcars(mk('H1', '#DFBABS011DA_S       UAAAEPWA2'), dec)).toBeNull();
  });
  it('B6 "/…" body (one Text item echoing the body) → null', () => {
    expect(decodeAcars(mk('B6', '/CPDLC something'), dec)).toBeNull();
    expect(decodeAcars(mk('B6', '/ABC'), dec)).toBeNull();
  });
});
