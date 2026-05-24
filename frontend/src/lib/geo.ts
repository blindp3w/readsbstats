// Great-circle geometry helpers — port of `src/readsbstats/geo.py`. Same
// formulas; same EARTH_RADIUS_NM. Used by the Flight detail header to
// compute the at-max-distance position's bearing without making the
// backend pre-compute it.

export const EARTH_RADIUS_NM = 3440.065;

const deg2rad = (d: number) => (d * Math.PI) / 180;
const rad2deg = (r: number) => (r * 180) / Math.PI;

export function haversineNm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const p1 = deg2rad(lat1);
  const p2 = deg2rad(lat2);
  const dp = deg2rad(lat2 - lat1);
  const dl = deg2rad(lon2 - lon1);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return EARTH_RADIUS_NM * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

// Initial bearing in degrees (0 = N, clockwise) from receiver to target.
export function bearingFromReceiver(
  recLat: number,
  recLon: number,
  lat: number,
  lon: number,
): number {
  const p1 = deg2rad(recLat);
  const p2 = deg2rad(lat);
  const dl = deg2rad(lon - recLon);
  const x = Math.sin(dl) * Math.cos(p2);
  const y = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (rad2deg(Math.atan2(x, y)) + 360) % 360;
}
