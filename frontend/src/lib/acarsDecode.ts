import type { MessageDecoder } from '@airframes/acars-decoder';
import type { Vdl2Message } from '@/lib/types';

// Normalized, display-ready decode result. `remaining` is the undecoded tail
// ("Remaining Text"); Layout A does not render it (the raw <pre> shows it), but
// it is part of the contract for future layouts/tests.
//
// The optional fields carry extra airframes metadata for richer layouts; every
// existing consumer (acarsLocal, MessageList) only needs description/items.
export interface DecodedAcars {
  description: string;
  items: { label: string; value: string; type?: string; code?: string }[];
  remaining?: string;
  // Plugin-specific structured fields (airframes `res.raw`), untrusted shape.
  raw?: Record<string, unknown>;
  // How much of the body the plugin understood (airframes `res.decoder.decodeLevel`).
  level?: 'full' | 'partial' | 'none';
}

const LEVELS = new Set(['full', 'partial', 'none']);

// Pure: decode one message with an injected decoder. Never throws; returns null
// when there is nothing useful to show (no body/label, decoder reports
// not-decoded, decoding throws, or the result merely echoes the body — e.g.
// label B6 "/…" → "CPDLC Message" + one Text item equal to the body minus its
// leading "/") so the caller falls back to the raw body / local decoders.
//
// `sublabel` is forwarded when present, but the text is deliberately NOT
// re-prefixed with "#DFB"/"#M1B": that breaks ARINC-702 CRC decoding and MFI
// bodies, and the plugins already list unprefixed preambles.
export function decodeAcars(msg: Vdl2Message, decoder: MessageDecoder): DecodedAcars | null {
  const text = msg.body;
  const label = msg.label;
  if (!text || !label) return null;
  let res;
  try {
    res = decoder.decode(msg.sublabel ? { label, text, sublabel: msg.sublabel } : { label, text });
  } catch {
    return null;
  }
  if (!res || !res.decoded) return null;
  const description = res.formatted?.description ?? '';
  if (!description || description.toLowerCase() === 'not decoded') return null;
  const echoes = new Set([text.trim(), text.slice(1).trim()].filter((v) => v !== ''));
  const items = (res.formatted?.items ?? [])
    .map((it) => {
      const item: DecodedAcars['items'][number] = {
        label: String(it.label ?? ''),
        value: String(it.value ?? ''),
      };
      if (it.type != null) item.type = String(it.type);
      if (it.code != null) item.code = String(it.code);
      return item;
    })
    .filter((it) => it.label !== '' || it.value !== '')
    .filter((it) => !echoes.has(it.value.trim()));
  // Nothing but a description left: no information beyond the raw body.
  if (items.length === 0) return null;
  const tail = res.remaining?.text;
  const remaining = tail && tail !== text ? tail : undefined;
  const out: DecodedAcars = { description, items, remaining };
  if (res.raw && typeof res.raw === 'object') out.raw = res.raw as Record<string, unknown>;
  const level = res.decoder?.decodeLevel;
  if (level && LEVELS.has(level)) out.level = level;
  return out;
}
