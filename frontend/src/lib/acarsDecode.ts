import type { MessageDecoder } from '@airframes/acars-decoder';
import type { Vdl2Message } from '@/lib/types';

// Normalized, display-ready decode result. `remaining` is the undecoded tail
// ("Remaining Text"); Layout A does not render it (the raw <pre> shows it), but
// it is part of the contract for future layouts/tests.
export interface DecodedAcars {
  description: string;
  items: { label: string; value: string }[];
  remaining?: string;
}

// Pure: decode one message with an injected decoder. Never throws; returns null
// when there is nothing useful to show (no body/label, decoder reports
// not-decoded, or decoding throws) so the caller falls back to the raw body.
export function decodeAcars(msg: Vdl2Message, decoder: MessageDecoder): DecodedAcars | null {
  const text = msg.body;
  const label = msg.label;
  if (!text || !label) return null;
  let res;
  try {
    res = decoder.decode({ label, text });
  } catch {
    return null;
  }
  if (!res || !res.decoded) return null;
  const description = res.formatted?.description ?? '';
  if (!description || description.toLowerCase() === 'not decoded') return null;
  const items = (res.formatted?.items ?? [])
    .map((it) => ({ label: String(it.label ?? ''), value: String(it.value ?? '') }))
    .filter((it) => it.label !== '' || it.value !== '');
  const tail = res.remaining?.text;
  const remaining = tail && tail !== text ? tail : undefined;
  return { description, items, remaining };
}
