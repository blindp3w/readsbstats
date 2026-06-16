// Narrow an unknown thrown value to a display string. Prefers `Error.message`,
// falls back to `String()` for non-Error throws (strings, plain objects).
// Centralises the `e instanceof Error ? e.message : String(e)` pattern used at
// error boundaries (e.g. the ACARS panel) so a non-Error rejection can never
// render `undefined`.
export function errMsg(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  return String(e);
}
