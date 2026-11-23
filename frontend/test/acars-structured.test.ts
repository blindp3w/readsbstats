import { describe, it, expect } from 'vitest';
import { MessageDecoder } from '@airframes/acars-decoder';
import { decodeAcars, type DecodedAcars } from '@/lib/acarsDecode';
import {
  classifyItem,
  formatPosition,
  parseRoute,
  structureItems,
} from '@/lib/acarsStructured';
import type { Vdl2Message } from '@/lib/types';

const dec = new MessageDecoder();
const mk = (label: string, body: string, extra: Partial<Vdl2Message> = {}) =>
  ({ id: 1, ts: 1, label, body, ...extra }) as Vdl2Message;

function withCrc(data: string): string {
  let crc = 0xffff;
  for (const ch of data) {
    crc ^= ch.charCodeAt(0) << 8;
    for (let i = 0; i < 8; i++) crc = crc & 0x8000 ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
  }
  return data + ((crc ^ 0xffff) & 0xffff).toString(16).toUpperCase().padStart(4, '0');
}

const item = (type: string | undefined, label: string, value: string) => ({ type, label, value });

describe('formatPosition', () => {
  it('formats the airframes coordinate string', () => {
    expect(formatPosition('52.135 N, 20.028 E')).toBe('52.135°N 20.028°E');
    expect(formatPosition('33.9 S, 151.2 W')).toBe('33.9°S 151.2°W');
  });
  it('rejects junk and out-of-range values', () => {
    expect(formatPosition('hello')).toBeNull();
    expect(formatPosition('95.000 N, 20.000 E')).toBeNull();
    expect(formatPosition('52.0 N, 200.0 E')).toBeNull();
    expect(formatPosition('<img src=x> N, 1 E')).toBeNull();
  });
});

describe('parseRoute', () => {
  it('splits waypoints and separates time detail', () => {
    const r = parseRoute('ABESI@08:53:21 > ASKIL@09:00:00 > XIMAL');
    expect(r?.name).toBe('');
    expect(r?.waypoints).toEqual([
      { name: 'ABESI', detail: '@08:53:21' },
      { name: 'ASKIL', detail: '@09:00:00' },
      { name: 'XIMAL', detail: '' },
    ]);
  });
  it('extracts a procedure-name prefix', () => {
    const r = parseRoute('WA01: WA02 > WA03');
    expect(r?.name).toBe('WA01');
    expect(r?.waypoints.map((w) => w.name)).toEqual(['WA02', 'WA03']);
  });
  it('handles "starting at" and unknown (>>) waypoints', () => {
    expect(parseRoute('SID(29) starting at ABC')?.waypoints.map((w) => w.name)).toEqual(['ABC']);
    expect(parseRoute('A >> B')?.waypoints.map((w) => w.name)).toEqual(['A', '?', 'B']);
    expect(parseRoute('>> B')?.waypoints.map((w) => w.name)).toEqual(['?', 'B']);
  });
  it('keeps coordinates in the detail', () => {
    const r = parseRoute('FIX(52.1 N, 20.0 E)[090° 12nm] > NEXT');
    expect(r?.waypoints[0]).toEqual({ name: 'FIX', detail: '(52.1 N, 20.0 E)[090° 12nm]' });
  });
  it('caps very long routes', () => {
    const r = parseRoute(Array.from({ length: 100 }, (_, i) => `W${i}`).join(' > '));
    expect(r?.waypoints).toHaveLength(40);
    expect(r?.more).toBe(60);
  });
  it('returns null for empty input', () => {
    expect(parseRoute('  ')).toBeNull();
  });
});

describe('classifyItem', () => {
  it('unknown / missing type -> chip', () => {
    expect(classifyItem(item(undefined, 'X', 'y')).kind).toBe('chip');
    expect(classifyItem(item('icao', 'Origin', 'EPWA')).kind).toBe('chip');
  });
  it('malformed values of known types fall back to chip', () => {
    expect(classifyItem(item('aircraft_position', 'Pos', 'garbage')).kind).toBe('chip');
    expect(classifyItem(item('gross_weight', 'GW', 'heavy')).kind).toBe('chip');
    expect(classifyItem(item('runway_landing_data', 'L', 'no colon here')).kind).toBe('chip');
  });
  it('position', () => {
    expect(classifyItem(item('aircraft_position', 'Aircraft Position', '52.135 N, 20.028 E'))).toEqual({
      kind: 'position',
      text: '52.135°N 20.028°E',
      label: 'Aircraft Position',
    });
  });
  it('figures: gross weight and fuel', () => {
    expect(classifyItem(item('gross_weight', 'Gross Weight', '123400 pounds'))).toEqual({
      kind: 'figure',
      label: 'Gross Weight',
      value: '123,400 lb',
    });
    expect(classifyItem(item('fuel_on_board', 'Fuel On Board', '12400'))).toMatchObject({
      kind: 'figure',
      value: '12,400',
    });
    expect(classifyItem(item('lbs', 'Out Fuel', '9000 lbs'))).toMatchObject({ value: '9,000 lb' });
  });
  it('landing data uses raw structured entries when they match', () => {
    const raw = {
      landing_runways: [
        {
          runway: '29',
          length: 3690,
          flaps: 30,
          headwind: -4,
          crosswind: 6,
          crosswindSide: 'R',
          vref: 135,
          vtarget: 140,
          additive: 5,
          stoppingMargins: { DRY: 1200 },
        },
      ],
    };
    const seg = classifyItem(item('runway_landing_data', 'L', '29: whatever'), raw);
    expect(seg.kind).toBe('landing');
    if (seg.kind !== 'landing') return;
    expect(seg.rows[0].runway).toBe('29');
    const map = Object.fromEntries(seg.rows[0].parts.map((p) => [p.label, p.value]));
    expect(map).toMatchObject({
      Length: '3,690 ft',
      Flaps: '30',
      Tailwind: '4 kt',
      Crosswind: '6 kt right',
      VREF: '135 kt',
      VTGT: '140 kt (+5)',
      'Margin DRY': '1,200 ft',
    });
  });
  it('landing data without a matching raw entry falls back to the text', () => {
    const seg = classifyItem(item('runway_landing_data', 'L', '11: 2800 feet'), { landing_runways: 'junk' });
    expect(seg).toEqual({
      kind: 'landing',
      rows: [{ runway: '11', parts: [{ label: 'Data', value: '2800 feet' }] }],
    });
  });
  it('ignores hostile raw shapes', () => {
    const raw = { landing_runways: [null, 5, { runway: 29, length: 'x' }, { runway: '11', length: NaN }] };
    const seg = classifyItem(item('runway_landing_data', 'L', '11: 2800 feet'), raw);
    expect(seg).toMatchObject({ kind: 'landing', rows: [{ parts: [{ label: 'Data' }] }] });
  });
});

describe('structureItems with the real decoder', () => {
  it('H1 POSN: position, route, plain chips for the rest', () => {
    const d = decodeAcars(mk('H1', withCrc('POSN52081E020017,ABESI,085321,370,ASKIL,090000,XIMAL,M52,28049,1070'), { sublabel: 'M1' }), dec)!;
    const segs = structureItems(d);
    const kinds = segs.map((s) => s.kind);
    expect(kinds).toContain('position');
    expect(kinds).toContain('route');
    expect(kinds).toContain('chip');
    const route = segs.find((s) => s.kind === 'route');
    expect(route && route.kind === 'route' && route.waypoints.map((w) => w.name)).toEqual(['ABESI', 'ASKIL', 'XIMAL']);
  });
  it('label 11 landing runways merge into one table', () => {
    const d = decodeAcars(mk('11', 'LRW01,EPWA LANDING RUNWAYS,\r\n11  2800\r\n29  3690\r\n'), dec)!;
    const segs = structureItems(d);
    const landing = segs.filter((s) => s.kind === 'landing');
    expect(landing).toHaveLength(1);
    expect(landing[0].kind === 'landing' && landing[0].rows.map((r) => r.runway)).toEqual(['11', '29']);
    expect(landing[0].kind === 'landing' && landing[0].rows[0].parts[0]).toEqual({ label: 'Length', value: '2,800 ft' });
  });
  it('is cached per decoded object', () => {
    const d: DecodedAcars = { description: 'x', items: [item('icao', 'A', 'B')] };
    expect(structureItems(d)).toBe(structureItems(d));
  });
});
