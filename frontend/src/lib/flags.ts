// Mirrors the bit constants in config.py and the precedence rules used
// in v1 templates and table-utils.js. Do NOT renumber — these are persisted
// indirectly through filters (?flags=military).

export const FLAG_MILITARY = 1;
export const FLAG_INTERESTING = 2;
export const FLAG_PIA = 4;
export const FLAG_LADD = 8;
export const FLAG_ANONYMOUS = 16;

export type FlagFilter = 'military' | 'interesting' | 'anonymous';

// Precedence (matches notifier / Telegram alerts):
// military > interesting > anonymous > none.
export function primaryFlagLabel(flags: number | null | undefined): FlagFilter | null {
  if (!flags) return null;
  if (flags & FLAG_MILITARY) return 'military';
  if (flags & FLAG_INTERESTING) return 'interesting';
  if (flags & FLAG_ANONYMOUS) return 'anonymous';
  return null;
}
