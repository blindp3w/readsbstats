// Heuristic human categories for VDL2/ACARS message bodies, keyed by body prefix.
//
// The 2-char ACARS *label* (vdl2Labels.ts) says little for H1 — 87% of this feed —
// and the client airframes decoder only decodes ~9% of H1. The body prefix is what
// actually distinguishes ACMS vs engine vs maintenance vs route, so a row that would
// otherwise be a wall of raw text gets a meaningful category chip.
//
// Names are best-effort and vendor/airline-specific (Honeywell/Teledyne/airline,
// inferred from the live LOT/EPWA feed) — honest-generic where unsure, like
// vdl2Labels.ts's "Airline-defined report". Display-only and fail-soft: an unknown
// prefix returns null and the row renders exactly as today.
//
// No key is a prefix of another, so first-match == longest-match. Route messages
// (#M1BPOS /RP:, RTE) intentionally have no "Route" entry — they get the richer
// filed_route line, and MessageList suppresses the chip when filed_route is present.

export const VDL2_BODY_KINDS: Record<string, string> = {
  '#DFB': 'ACMS report',
  '#CFB': 'Maintenance (CMS)',
  '#T8B': 'Engine report',
  '#T1B': 'AID report',
  '#T2B': 'AID report',
  '#T3B': 'AID report',
  '#T6B': 'AID report',
  '#EIB': 'Brake/system report',
  '#M1B': 'FMS position/route',
  OHMA: 'Boeing OHMA',
  '01IC': 'Performance report',
  '01 W': 'Weather request',
  '59,G,': 'Position report',
};

export function bodyKind(body: string | null | undefined): string | null {
  if (!body) return null;
  for (const [prefix, kind] of Object.entries(VDL2_BODY_KINDS)) {
    if (body.startsWith(prefix)) return kind;
  }
  return null;
}
