import type { Vdl2Message } from '@/lib/types';
import type { DecodedAcars } from '@/lib/acarsDecode';

// Local decoders for ACARS formats the airframes decoder leaves undecoded on this
// feed. Each regex was validated against the real 90-day feed (matched/total in
// the per-format comments below). Pure and never throwing: returns null when
// there is no body/label, the label has no local decoder, or the body doesn't
// match — the caller then falls back to the raw body. Decoded values are
// untrusted upstream text; they are only ever rendered as React children.
//
// Order of use: airframes first, these second (see hooks/useAcarsDecoder).

type Item = { label: string; value: string };
type Decoder = (text: string) => DecodedAcars | null;

const ok = (description: string, items: Item[]): DecodedAcars => ({ description, items });

function hms(hh: string, mm: string, ss: string): string | null {
  return +hh <= 23 && +mm <= 59 && +ss <= 59 ? `${hh}:${mm}:${ss} UTC` : null;
}

// SA media advisory — 1176/1176. Algorithm follows libacars `media-adv.c`.
const LINKS: Record<string, string> = {
  V: 'VHF ACARS',
  S: 'Default SATCOM',
  H: 'HF',
  G: 'Globalstar',
  C: 'ICO',
  '2': 'VDL2',
  X: 'Inmarsat Aero',
  I: 'Iridium',
};
const SA_RE = /^0([EL])([VSHGC2XI])(\d{6})([VSHGC2XI]*)(?:\/([\s\S]*))?$/;

const decodeSA: Decoder = (text) => {
  const m = SA_RE.exec(text);
  if (!m) return null;
  const time = hms(m[3].slice(0, 2), m[3].slice(2, 4), m[3].slice(4, 6));
  if (!time) return null;
  const items: Item[] = [
    { label: 'Link', value: `${LINKS[m[2]]} ${m[1] === 'E' ? 'established' : 'lost'}` },
    { label: 'Time', value: time },
  ];
  if (m[4]) items.push({ label: 'Available', value: [...m[4]].map((c) => LINKS[c]).join(', ') });
  if (m[5]) items.push({ label: 'Text', value: m[5] });
  return ok('Media advisory', items);
};

// B9 D-ATIS request — 1242/1243. The 3-digit field and CRC are not shown. The
// A/D letter is read as Arrival/Departure; any other letter is kept raw.
const B9_RE = /^(?:\/([A-Z]{4})\.)?TI2\/(\d{3})([A-Z]{4}|[A-Z]{3} )([A-Z])([0-9A-F]{4})$/;

const decodeB9: Decoder = (text) => {
  const m = B9_RE.exec(text);
  if (!m) return null;
  const kind = m[4] === 'A' ? 'Arrival' : m[4] === 'D' ? 'Departure' : m[4];
  return ok('D-ATIS request', [
    { label: 'Airport', value: m[3].trim() },
    { label: 'ATIS', value: kind },
  ]);
};

// 5U weather request — three forms cover 473/497.
const WX_PREFIX_RE = /^(SA|FT|FC|FS|FA) +([A-Z]{3,4}(?:, *[A-Z]{3,4})*)\s*$/;
const WX_SUFFIX_RE = /^([A-Z]{3,4}(?: +[A-Z]{3,4})*) *-(SA|FT|FC|FS)\s*$/;
const WX_ROUTE_RE = /WXRQ[^\n\r]*?\b([A-Z]{4})\/([A-Z]{4})\b/;
const WX_REG_RE = / \.([A-Z0-9-]+)/;

const wxType = (code: string): string =>
  code === 'SA' ? 'METAR' : code === 'FT' || code === 'FC' ? 'TAF' : code;

const decode5U: Decoder = (text) => {
  if (text.includes('WXRQ')) {
    const route = WX_ROUTE_RE.exec(text);
    if (!route) return null;
    const reg = WX_REG_RE.exec(text);
    const stations = [...text.matchAll(/\/STA ([A-Z]{4})/g)].map((s) => s[1]);
    const items: Item[] = [{ label: 'Route', value: `${route[1]} → ${route[2]}` }];
    if (reg) items.push({ label: 'Aircraft', value: reg[1] });
    if (stations.length) items.push({ label: 'Stations', value: stations.join(', ') });
    return ok('Weather request', items);
  }
  const pre = WX_PREFIX_RE.exec(text);
  if (pre) {
    return ok('Weather request', [
      { label: 'Stations', value: pre[2].split(/, */).join(', ') },
      { label: 'Type', value: wxType(pre[1]) },
    ]);
  }
  const suf = WX_SUFFIX_RE.exec(text);
  if (suf) {
    return ok('Weather request', [
      { label: 'Stations', value: suf[1].split(/ +/).join(', ') },
      { label: 'Type', value: wxType(suf[2]) },
    ]);
  }
  return null;
};

// 8A/8C/8F/8S Ryanair door events — 1231/1231. There is NO public spec: the
// meaning was INFERRED by correlating with ADS-B. C = door closed (seen before
// departure), O = door opened (seen after arrival). The door comes from the
// label (F forward, A aft, C cargo, S service), so everything is flagged
// "(probable)". `----` means no airport was reported.
const DOOR_RE = /^(\d{2})(\d{2})(\d{2}),([OC]),([A-Z]{4}|----)\s*$/;
const DOORS: Record<string, string> = { F: 'forward', A: 'aft', C: 'cargo', S: 'service' };

function decodeDoor(label: string): Decoder {
  return (text) => {
    const m = DOOR_RE.exec(text);
    if (!m) return null;
    const time = hms(m[1], m[2], m[3]);
    if (!time) return null;
    const items: Item[] = [
      { label: 'Door', value: `${DOORS[label.charAt(1)]} (probable)` },
      { label: 'Time', value: time },
    ];
    if (m[5] !== '----') items.push({ label: 'Airport', value: m[5] });
    return ok(m[4] === 'C' ? 'Door closed' : 'Door opened', items);
  };
}

// 26 Air France ETA report — 142/142. The separator before the ETA may be a
// bare `\r`, hence `\s*`.
const AF_RE = /^(\d{4})([A-Z]{4})([A-Z0-9]{2,3}\d{1,4}[A-Z]?)\/(\d{2})\s*(\d{4})([A-Z]{4})(\w*)$/;

const decode26: Decoder = (text) => {
  const m = AF_RE.exec(text);
  if (!m) return null;
  const items: Item[] = [
    { label: 'Flight', value: m[3] },
    { label: 'Route', value: `${m[2]} → ${m[6]}` },
    { label: 'ETA', value: `${m[5].slice(0, 2)}:${m[5].slice(2)}` },
  ];
  if (m[7]) items.push({ label: 'Runway', value: m[7] });
  return ok('ETA report', items);
};

// 20 KLM route report — 41/41. Other label-20 bodies (e.g. `POS…`) are left to
// airframes.
const KLM_RE = /^RST\d{2}[A-Z](\d{2})(\d{6})([A-Z0-9]{3,8})\s+([A-Z]{4})([A-Z]{4})$/;

const decode20: Decoder = (text) => {
  const m = KLM_RE.exec(text);
  if (!m) return null;
  return ok('Route report', [
    { label: 'Flight', value: m[3] },
    { label: 'Route', value: `${m[4]} → ${m[5]}` },
  ]);
};

const DECODERS: Record<string, Decoder> = {
  SA: decodeSA,
  B9: decodeB9,
  '5U': decode5U,
  '8A': decodeDoor('8A'),
  '8C': decodeDoor('8C'),
  '8F': decodeDoor('8F'),
  '8S': decodeDoor('8S'),
  '26': decode26,
  '20': decode20,
};

export function decodeLocal(msg: Vdl2Message): DecodedAcars | null {
  const text = msg.body;
  const label = msg.label?.toUpperCase();
  if (!text || !label) return null;
  const dec = Object.hasOwn(DECODERS, label) ? DECODERS[label] : undefined;
  if (!dec) return null;
  try {
    return dec(text);
  } catch {
    return null;
  }
}
