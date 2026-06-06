// Pure fit-key for RouteMap's fitBounds effect. A track whose shape changes
// should re-fit; identity churn on the same shape should not. The key folds in
// the point count plus BOTH coordinates (lng AND lat) of the first and last
// point. Keying on longitude alone (the pre-BUG-2 behaviour) made two tracks
// that shared endpoint longitudes but differed in latitude collide, so the map
// skipped re-fitting.
//
// Points are [lng, lat] (MapLibre order — the API boundary has already swapped
// the backend's [lat, lon]).
export function routeFitKey(points: readonly [number, number][]): string {
  const len = points.length;
  if (len === 0) return '0';
  const first = points[0];
  const last = points[len - 1];
  return `${len}-${first[0]},${first[1]}-${last[0]},${last[1]}`;
}
