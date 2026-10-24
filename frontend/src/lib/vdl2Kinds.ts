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
// Under dumpvdl2 (libacars) the H1 prefix `#` + 2-char sublabel + `B` (and an optional
// `/` + 2-char MFI) is stripped out of the body into separate sublabel/mfi fields, so
// the `#DFB`-style keys below never match the stored body. bodyKind() therefore tries
// the raw body first and only then the downlink text rebuilt from the sublabel. Raw
// first matters: OHMA rows carry sublabel T1 but their body starts with `OHMA`, and
// must stay 'Boeing OHMA' rather than become '#T1B' -> 'AID report'. MFI is not
// rebuilt — no kind prefix depends on it.
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
  // NB: 59,G is intentionally NOT here — its prefix conflates two message types
  // that split by label (36 = airborne position, 37 = airport/runway status), so
  // bodyKind() disambiguates it via the label argument rather than the prefix.
};

export function bodyKind(
  body: string | null | undefined,
  label?: string | null,
  sublabel?: string | null,
): string | null {
  if (!body) return null;
  if (body.startsWith('59,G,')) {
    // Ambiguous prefix: label 37 is the airport/runway status sub-form; label 36
    // (and anything else/unknown) is the airborne position telemetry.
    return (label ?? '').toUpperCase() === '37' ? 'Ground report' : 'Position report';
  }
  const match = (text: string): string | null => {
    for (const [prefix, kind] of Object.entries(VDL2_BODY_KINDS)) {
      if (text.startsWith(prefix)) return kind;
    }
    return null;
  };
  return match(body) ?? (sublabel ? match(`#${sublabel}B${body}`) : null);
}
