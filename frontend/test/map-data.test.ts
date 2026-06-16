/**
 * Unit tests for lib/mapData.dedupeFreshestByIcao — the freshest-per-icao
 * collapse used by LiveMap, previously inline and untestable behind the map
 * mock.
 */
import { describe, it, expect } from 'vitest';
import { dedupeFreshestByIcao } from '@/lib/mapData';

describe('dedupeFreshestByIcao', () => {
  it('keeps the highest-ts row per icao_hex', () => {
    const rows = [
      { icao_hex: 'aaa', ts: 100, flight_id: 1 },
      { icao_hex: 'aaa', ts: 200, flight_id: 2 }, // newer flight, same airframe
      { icao_hex: 'bbb', ts: 150, flight_id: 3 },
    ];
    const out = dedupeFreshestByIcao(rows);
    expect(out).toHaveLength(2);
    const aaa = out.find((r) => r.icao_hex === 'aaa');
    expect(aaa?.ts).toBe(200);
    expect(aaa?.flight_id).toBe(2);
    expect(out.find((r) => r.icao_hex === 'bbb')?.flight_id).toBe(3);
  });

  it('is order-independent (later older row does not overwrite a newer one)', () => {
    const rows = [
      { icao_hex: 'aaa', ts: 200, flight_id: 2 },
      { icao_hex: 'aaa', ts: 100, flight_id: 1 }, // older arrives second
    ];
    const out = dedupeFreshestByIcao(rows);
    expect(out).toHaveLength(1);
    expect(out[0].ts).toBe(200);
  });

  it('returns [] for an empty list', () => {
    expect(dedupeFreshestByIcao([])).toEqual([]);
  });

  it('passes through distinct icaos unchanged', () => {
    const rows = [
      { icao_hex: 'aaa', ts: 1 },
      { icao_hex: 'bbb', ts: 2 },
      { icao_hex: 'ccc', ts: 3 },
    ];
    expect(dedupeFreshestByIcao(rows)).toHaveLength(3);
  });
});
