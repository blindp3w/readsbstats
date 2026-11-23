import type { DecodedAcars } from '@/lib/acarsDecode';

// Pure classification of decoded ACARS items (airframes `formatted.items[].type`)
// into richer render segments for MessageList. Everything here is defensive:
// item values and `raw` come from a third-party decoder fed untrusted bodies, so
// each field is type-guarded and every segment carries only plain strings/numbers
// (rendered as React text, never HTML). Anything unrecognised stays a `chip` —
// the existing label/value rendering.
//
// Real item shapes (airframes 1.9.x ResultFormatter):
//   aircraft_position  "52.135 N, 20.028 E"
//   aircraft_route     "ABESI@08:53:21 > ASKIL@09:00:00 > XIMAL"  /  "NAME(RWY): A > B"
//   runway_landing_data "29/AB -R2: 3690 feet"  (+ raw.landing_runways[] structured)
//   gross_weight       "123400 pounds"
//   fuel_on_board / fuel_burned / fuel_remaining  "12400" ; lbs  "12400 lbs"

export type DecodedItem = DecodedAcars['items'][number];

export interface Waypoint {
  name: string;
  detail: string; // "(lat, lon)", "[brg dist]", "@time" tail — dim, may be ''
}

export interface LandingRow {
  runway: string;
  parts: { label: string; value: string }[];
}

export type Segment =
  | { kind: 'chip'; item: DecodedItem }
  | { kind: 'position'; text: string; label: string }
  | { kind: 'route'; label: string; name: string; waypoints: Waypoint[]; more: number }
  | { kind: 'figure'; label: string; value: string }
  | { kind: 'landing'; rows: LandingRow[] };

const MAX_WAYPOINTS = 40;
const MAX_ROWS = 24;

const POS_RE = /^(\d{1,3}(?:\.\d+)?) ([NS]), (\d{1,3}(?:\.\d+)?) ([EW])$/;

// "52.135 N, 20.028 E" -> "52.135°N 20.028°E". Null when it isn't a valid fix.
export function formatPosition(value: string): string | null {
  const m = POS_RE.exec(value.trim());
  if (!m) return null;
  const lat = Number(m[1]);
  const lon = Number(m[3]);
  if (!(lat <= 90) || !(lon <= 180)) return null;
  return `${m[1]}°${m[2]} ${m[3]}°${m[4]}`;
}

export function parseRoute(
  value: string,
): { name: string; waypoints: Waypoint[]; more: number } | null {
  let body = value.trim();
  if (!body) return null;
  let name = '';
  // Optional "NAME(RWY): " / "NAME(RWY) starting at " prefix — only when it
  // precedes the first waypoint separator.
  const sep = /: | starting at /.exec(body);
  const firstArrow = body.indexOf(' > ');
  if (sep && sep.index > 0 && (firstArrow === -1 || sep.index < firstArrow)) {
    name = body.slice(0, sep.index).trim();
    body = body.slice(sep.index + sep[0].length).trim();
  }
  // The library collapses an empty waypoint name to ">>" (unknown waypoint).
  body = body.replace(/^>>\s*/, '? > ').replace(/>>/g, ' > ? > ');
  const tokens = body
    .split(' > ')
    .map((t) => t.trim())
    .map((t) => (t === '' ? '?' : t));
  if (tokens.length === 0 || (tokens.length === 1 && tokens[0] === '?' && !name)) return null;
  const waypoints = tokens.slice(0, MAX_WAYPOINTS).map((t) => {
    const cut = t.search(/[(@[]/);
    return cut > 0
      ? { name: t.slice(0, cut).trim(), detail: t.slice(cut).trim() }
      : { name: t, detail: '' };
  });
  return { name, waypoints, more: Math.max(0, tokens.length - waypoints.length) };
}

const NUM_RE = /^(-?\d+(?:\.\d+)?)(?:\s*(pounds|lbs))?$/;
const FIGURE_TYPES = new Set([
  'gross_weight',
  'fuel_on_board',
  'fuel_burned',
  'fuel_remaining',
  'lbs',
]);

function fmtInt(n: number): string {
  return n.toLocaleString('en-US', { maximumFractionDigits: 2 });
}

export function parseFigure(item: DecodedItem): { label: string; value: string } | null {
  const m = NUM_RE.exec(item.value.trim());
  if (!m) return null;
  const n = Number(m[1]);
  if (!Number.isFinite(n)) return null;
  return { label: item.label, value: `${fmtInt(n)}${m[2] ? ' lb' : ''}` };
}

const isRec = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);
const str = (v: unknown): string | undefined => (typeof v === 'string' && v !== '' ? v : undefined);
const num = (v: unknown): number | undefined =>
  typeof v === 'number' && Number.isFinite(v) ? v : undefined;

function landingParts(e: Record<string, unknown>): LandingRow['parts'] {
  const out: LandingRow['parts'] = [];
  const add = (label: string, value: string) => out.push({ label, value });
  const length = num(e.length);
  if (length !== undefined) add('Length', `${fmtInt(length)} ft`);
  const flaps = num(e.flaps);
  if (flaps !== undefined) add('Flaps', String(flaps));
  const head = num(e.headwind);
  if (head !== undefined) add(head < 0 ? 'Tailwind' : 'Headwind', `${Math.abs(head)} kt`);
  const cross = num(e.crosswind);
  if (cross !== undefined) {
    const side = e.crosswindSide === 'L' ? ' left' : e.crosswindSide === 'R' ? ' right' : '';
    add('Crosswind', `${cross} kt${side}`);
  }
  const vref = num(e.vref);
  if (vref !== undefined) add('VREF', `${vref} kt`);
  const vt = num(e.vtarget);
  if (vt !== undefined) {
    const ad = num(e.additive);
    add('VTGT', `${vt} kt${ad === undefined ? '' : ` (${ad < 0 ? '' : '+'}${ad})`}`);
  }
  const mrlw = num(e.maxLandingWeight);
  if (mrlw !== undefined) add('MRLW', fmtInt(mrlw));
  const ldw = num(e.landingWeight);
  if (ldw !== undefined) add('LDW', `${fmtInt(ldw)} lb`);
  if (isRec(e.stoppingMargins)) {
    for (const [setting, feet] of Object.entries(e.stoppingMargins).slice(0, 8)) {
      const f = num(feet);
      if (f !== undefined) add(`Margin ${setting}`.slice(0, 40), `${fmtInt(f)} ft`);
    }
  }
  if (Array.isArray(e.notes)) {
    const notes = e.notes.filter((n): n is string => typeof n === 'string').slice(0, 4);
    if (notes.length) add('Notes', notes.join('; '));
  }
  return out;
}

// One landing item -> table row. Prefers the structured raw entry (matched by the
// "<runway>[ (<variant>)]: " prefix the formatter writes); falls back to the
// formatted text after the colon.
function landingRow(item: DecodedItem, raw: unknown): LandingRow | null {
  const value = item.value;
  const colon = value.indexOf(': ');
  if (colon <= 0) return null;
  const head = value.slice(0, colon);
  const entries = isRec(raw) && Array.isArray(raw.landing_runways) ? raw.landing_runways : [];
  for (const e of entries) {
    if (!isRec(e)) continue;
    const rwy = str(e.runway);
    if (!rwy) continue;
    const variant = str(e.variant);
    if (head === `${rwy}${variant ? ` (${variant})` : ''}`) {
      const parts = landingParts(e);
      if (parts.length) return { runway: head, parts };
    }
  }
  const rest = value.slice(colon + 2).trim();
  return rest ? { runway: head, parts: [{ label: 'Data', value: rest }] } : null;
}

export function classifyItem(item: DecodedItem, raw?: unknown): Segment {
  const chip: Segment = { kind: 'chip', item };
  switch (item.type) {
    case 'aircraft_position': {
      const text = formatPosition(item.value);
      return text ? { kind: 'position', text, label: item.label } : chip;
    }
    case 'aircraft_route': {
      const r = parseRoute(item.value);
      return r ? { kind: 'route', label: item.label, ...r } : chip;
    }
    case 'runway_landing_data': {
      const row = landingRow(item, raw);
      return row ? { kind: 'landing', rows: [row] } : chip;
    }
    default:
      if (item.type && FIGURE_TYPES.has(item.type)) {
        const f = parseFigure(item);
        return f ? { kind: 'figure', ...f } : chip;
      }
      return chip;
  }
}

// Classify every item and merge consecutive landing rows into one table.
// Results are cached per decoded object (decode results are cached per message
// id upstream, so the same object is re-rendered on every scroll frame).
const cache = new WeakMap<DecodedAcars, Segment[]>();

export function structureItems(decoded: DecodedAcars): Segment[] {
  const hit = cache.get(decoded);
  if (hit) return hit;
  const out: Segment[] = [];
  for (const it of decoded.items) {
    const seg = classifyItem(it, decoded.raw);
    const last = out[out.length - 1];
    if (seg.kind === 'landing' && last?.kind === 'landing') {
      if (last.rows.length < MAX_ROWS) last.rows.push(...seg.rows);
    } else {
      out.push(seg);
    }
  }
  cache.set(decoded, out);
  return out;
}
