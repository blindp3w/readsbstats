import { describe, it, expect } from 'vitest';
import { decodeLocal } from '@/lib/acarsLocal';
import type { Vdl2Message } from '@/lib/types';

const msg = (label: string | null, body: string | null): Vdl2Message =>
  ({ id: 1, ts: 1_749_000_000, label, body }) as Vdl2Message;

const items = (label: string, body: string) =>
  Object.fromEntries((decodeLocal(msg(label, body))?.items ?? []).map((i) => [i.label, i.value]));

describe('decodeLocal — guards', () => {
  it('returns null for null/empty body or label', () => {
    expect(decodeLocal(msg('SA', null))).toBeNull();
    expect(decodeLocal(msg('SA', ''))).toBeNull();
    expect(decodeLocal(msg(null, '0EV123456'))).toBeNull();
  });
  it('returns null for an unknown label', () => {
    expect(decodeLocal(msg('ZZ', '0EV123456'))).toBeNull();
  });
  it('accepts a lowercase label', () => {
    expect(decodeLocal(msg('sa', '0EV123456'))?.description).toBe('Media advisory');
  });
});

describe('decodeLocal — SA media advisory', () => {
  it('decodes an established link with time', () => {
    const d = decodeLocal(msg('SA', '0EV123456V'));
    expect(d?.description).toBe('Media advisory');
    expect(items('SA', '0EV123456V')).toEqual({
      Link: 'VHF ACARS established',
      Time: '12:34:56 UTC',
      Available: 'VHF ACARS',
    });
  });
  it('decodes a lost link with several available media and text', () => {
    expect(items('SA', '0LV000102VS2/hello')).toEqual({
      Link: 'VHF ACARS lost',
      Time: '00:01:02 UTC',
      Available: 'VHF ACARS, Default SATCOM, VDL2',
      Text: 'hello',
    });
  });
  it('omits Available and Text when empty', () => {
    expect(items('SA', '0E2235959')).toEqual({ Link: 'VDL2 established', Time: '23:59:59 UTC' });
    expect(items('SA', '0E2235959/')).toEqual({ Link: 'VDL2 established', Time: '23:59:59 UTC' });
  });
  it('covers every link code', () => {
    expect(items('SA', '0EH101010')['Link']).toBe('HF established');
    expect(items('SA', '0EG101010')['Link']).toBe('Globalstar established');
    expect(items('SA', '0EC101010')['Link']).toBe('ICO established');
    expect(items('SA', '0EX101010')['Link']).toBe('Inmarsat Aero established');
    expect(items('SA', '0EI101010')['Link']).toBe('Iridium established');
  });
  it('rejects version != 0, bad code, bad time, junk', () => {
    expect(decodeLocal(msg('SA', '1EV123456'))).toBeNull();
    expect(decodeLocal(msg('SA', '0EZ123456'))).toBeNull();
    expect(decodeLocal(msg('SA', '0EV240000'))).toBeNull();
    expect(decodeLocal(msg('SA', '0EV126000'))).toBeNull();
    expect(decodeLocal(msg('SA', '0EV125960'))).toBeNull();
    expect(decodeLocal(msg('SA', 'junk'))).toBeNull();
  });
});

describe('decodeLocal — B9 D-ATIS request', () => {
  it('decodes an arrival request with ICAO airport', () => {
    const body = '/EPWA.TI2/024EPWAA1234';
    expect(decodeLocal(msg('B9', body))?.description).toBe('D-ATIS request');
    expect(items('B9', body)).toEqual({ Airport: 'EPWA', ATIS: 'Arrival' });
  });
  it('decodes a departure request', () => {
    expect(items('B9', 'TI2/242EHAMD00AF')).toEqual({ Airport: 'EHAM', ATIS: 'Departure' });
  });
  it('decodes the IATA variant (trailing space trimmed)', () => {
    expect(items('B9', 'TI2/024CGN A2052')).toEqual({ Airport: 'CGN', ATIS: 'Arrival' });
  });
  it('keeps an unknown letter raw', () => {
    expect(items('B9', 'TI2/024CGN T2052')).toEqual({ Airport: 'CGN', ATIS: 'T' });
  });
  it('does not expose the 3-digit field or the CRC', () => {
    const vals = Object.values(items('B9', 'TI2/242EHAMD00AF')).join(' ');
    expect(vals).not.toContain('242');
    expect(vals).not.toContain('00AF');
  });
  it('rejects junk', () => {
    expect(decodeLocal(msg('B9', 'TI2/xx'))).toBeNull();
    expect(decodeLocal(msg('B9', 'hello'))).toBeNull();
  });
});

describe('decodeLocal — 5U weather request', () => {
  it('decodes the WXRQ form', () => {
    const body = '01 WXRQ   0267/23 EPWA/EHAM .SP-LYG\n/TYP 1/STA EHAM/STA EPPO/STA EDDB';
    expect(decodeLocal(msg('5U', body))?.description).toBe('Weather request');
    expect(items('5U', body)).toEqual({
      Route: 'EPWA → EHAM',
      Aircraft: 'SP-LYG',
      Stations: 'EHAM, EPPO, EDDB',
    });
  });
  it('handles \\r line breaks in the WXRQ form', () => {
    const body = '01 WXRQ   0267/23 EPWA/EHAM .SP-LYG\r/TYP 1/STA EHAM';
    expect(items('5U', body)).toEqual({
      Route: 'EPWA → EHAM',
      Aircraft: 'SP-LYG',
      Stations: 'EHAM',
    });
  });
  it('decodes the prefix form', () => {
    expect(items('5U', 'SA EPWA,EPWR')).toEqual({ Stations: 'EPWA, EPWR', Type: 'METAR' });
    expect(items('5U', 'SA EKBI, GDN, CPH')).toEqual({ Stations: 'EKBI, GDN, CPH', Type: 'METAR' });
    expect(items('5U', 'FT EPWA\r')['Type']).toBe('TAF');
    expect(items('5U', 'FC EPWA')['Type']).toBe('TAF');
    expect(items('5U', 'FS EPWA')['Type']).toBe('FS');
  });
  it('decodes the suffix form', () => {
    expect(items('5U', 'EPWA EPKK LHBP  YBE-SA')).toEqual({
      Stations: 'EPWA, EPKK, LHBP, YBE',
      Type: 'METAR',
    });
    expect(items('5U', 'EPWA-FT')['Type']).toBe('TAF');
  });
  it('rejects junk', () => {
    expect(decodeLocal(msg('5U', 'nothing here'))).toBeNull();
  });
});

describe('decodeLocal — Ryanair door events (8A/8C/8F/8S)', () => {
  it('decodes door closed on the forward door as probable', () => {
    const d = decodeLocal(msg('8F', '123456,C,EPWA'));
    expect(d?.description).toBe('Door closed');
    expect(items('8F', '123456,C,EPWA')).toEqual({
      Door: 'forward (probable)',
      Time: '12:34:56 UTC',
      Airport: 'EPWA',
    });
  });
  it('decodes door opened and maps each label to a door', () => {
    expect(decodeLocal(msg('8A', '101010,O,EGSS'))?.description).toBe('Door opened');
    expect(items('8A', '101010,O,EGSS')['Door']).toBe('aft (probable)');
    expect(items('8C', '101010,O,EGSS')['Door']).toBe('cargo (probable)');
    expect(items('8S', '101010,O,EGSS')['Door']).toBe('service (probable)');
  });
  it('omits Airport for ----', () => {
    expect(items('8F', '123456,C,----\r\n')).toEqual({
      Door: 'forward (probable)',
      Time: '12:34:56 UTC',
    });
  });
  it('rejects junk and bad times', () => {
    expect(decodeLocal(msg('8F', '123456,X,EPWA'))).toBeNull();
    expect(decodeLocal(msg('8F', '246000,C,EPWA'))).toBeNull();
    expect(decodeLocal(msg('8F', 'junk'))).toBeNull();
  });
});

describe('decodeLocal — 26 Air France ETA report', () => {
  it('decodes with runway and \\r separator', () => {
    const body = '1049EPWAAF1147/18\r1241LFPG27R';
    expect(decodeLocal(msg('26', body))?.description).toBe('ETA report');
    expect(items('26', body)).toEqual({
      Flight: 'AF1147',
      Route: 'EPWA → LFPG',
      ETA: '12:41',
      Runway: '27R',
    });
  });
  it('decodes without runway and with a newline', () => {
    expect(items('26', '1049EPWAAF1147/18\n1241LFPG')).toEqual({
      Flight: 'AF1147',
      Route: 'EPWA → LFPG',
      ETA: '12:41',
    });
  });
  it('rejects junk', () => {
    expect(decodeLocal(msg('26', 'nonsense'))).toBeNull();
  });
});

describe('decodeLocal — 20 KLM route report', () => {
  it('decodes an RST body', () => {
    const body = 'RST53A27145652KLM49N  EHAMEPWA';
    expect(decodeLocal(msg('20', body))?.description).toBe('Route report');
    expect(items('20', body)).toEqual({ Flight: 'KLM49N', Route: 'EHAM → EPWA' });
  });
  it('returns null for non-RST bodies (airframes handles POS)', () => {
    expect(decodeLocal(msg('20', 'POS N52123E021456'))).toBeNull();
  });
});
