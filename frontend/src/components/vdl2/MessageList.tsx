import { useFormat } from '@/hooks/useFormat';
import { Badge } from '@/components/ui/Badge';
import type { Vdl2Message } from '@/lib/types';

// Shared VDL2 message log rendering, used by the Vdl2 page (filterable feed) and
// the flight-detail ACARS panel. Bodies are untrusted upstream text rendered as
// plain React children (auto-escaped) — never raw HTML.
//
// When onHexClick/onRegClick are provided (the feed page), hex/reg render as
// filter buttons; otherwise (the flight panel) they render as plain text.
interface Props {
  messages: Vdl2Message[];
  onHexClick?: (hex: string) => void;
  onRegClick?: (reg: string) => void;
}

export function MessageList({ messages, onHexClick, onRegClick }: Props) {
  // Reactive timestamp formatter — re-renders when the 12h/24h clock-format
  // store toggles (a bare `fmtTs` import would snapshot the format at mount).
  const { fmtTs } = useFormat();
  return (
    <ul className="divide-y divide-[var(--color-border-default)]" data-testid="vdl2-list">
      {messages.map((m) => (
        <li key={m.id} className="py-2" data-testid="vdl2-message-row">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
            <span className="tabnum text-[var(--color-text-dim)]">{fmtTs(m.ts)}</span>
            {m.icao_hex &&
              (onHexClick ? (
                <button
                  type="button"
                  className="font-mono text-[var(--color-accent)] hover:underline"
                  onClick={() => onHexClick(m.icao_hex!)}
                  data-testid="vdl2-row-hex"
                >
                  {m.icao_hex}
                </button>
              ) : (
                <span className="font-mono" data-testid="vdl2-row-hex">
                  {m.icao_hex}
                </span>
              ))}
            {m.registration &&
              (onRegClick ? (
                <button
                  type="button"
                  className="font-mono hover:underline"
                  onClick={() => onRegClick(m.registration!)}
                >
                  {m.registration}
                </button>
              ) : (
                <span className="font-mono">{m.registration}</span>
              ))}
            {m.flight && <span className="font-mono">{m.flight}</span>}
            {m.label && <Badge variant="muted">{m.label}</Badge>}
            {m.dsta && <span className="text-[var(--color-text-dim)]">→ {m.dsta}</span>}
          </div>
          {m.body && (
            <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-xs text-[var(--color-text)]">
              {m.body}
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}
