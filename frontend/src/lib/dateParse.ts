// Strict YYYY-MM-DD parsing with round-trip validation.
//
// A bare regex + `new Date(y, mo, d)` silently rolls impossible dates over
// (2026-02-31 → Mar 3) and maps 2-digit years into the 1900s, so an edited URL
// or hand-typed value can query the wrong window with no visible error. Round-
// tripping through Date and comparing the parts back rejects those. audit 2026-06-15.
export function parseYMD(s: string): { y: number; mo: number; d: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]) - 1; // zero-based month, as Date expects
  const d = Number(m[3]);
  const dt = new Date(y, mo, d);
  if (dt.getFullYear() !== y || dt.getMonth() !== mo || dt.getDate() !== d) {
    return null;
  }
  return { y, mo, d };
}
