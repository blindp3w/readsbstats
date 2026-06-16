import { useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useFormat } from '@/hooks/useFormat';
import { Badge } from '@/components/ui/Badge';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { labelName } from '@/lib/vdl2Labels';
import { bodyKind } from '@/lib/vdl2Kinds';
import { useAcarsDecoder } from '@/hooks/useAcarsDecoder';
import type { DecodedAcars } from '@/lib/acarsDecode';
import type { Vdl2Message } from '@/lib/types';

// Shared VDL2 message log rendering, used by the Vdl2 page (filterable feed) and
// the flight-detail ACARS panel. Bodies are untrusted upstream text rendered as
// plain React children (auto-escaped) — never raw HTML. Decoded fields and the
// server-parsed filed route are likewise rendered as escaped children.
//
// The list is windowed with @tanstack/react-virtual: it owns a bounded scroll
// viewport (height/overflow via `scrollClassName`) and only mounts the visible
// rows, so the feed's unbounded "Load older" accumulation can't grow the DOM
// without limit. Row heights vary (decoded chips / body length / filed route),
// so each row reports its real height via `measureElement`.
//
// When onHexClick/onRegClick are provided (the feed page), hex/reg render as
// filter buttons; otherwise (the flight panel) they render as plain text.
//
// `decode` defaults to useAcarsDecoder() (lazy client-side decoder). Tests inject
// a synchronous decode fn so decoded rendering is deterministic. (In tests the
// virtualizer is mocked to render all rows — see test/setup.ts — so jsdom's
// zero-height layout can't hide rows.)
interface Props {
  messages: Vdl2Message[];
  onHexClick?: (hex: string) => void;
  onRegClick?: (reg: string) => void;
  decode?: (msg: Vdl2Message) => DecodedAcars | null;
  // Classes for the scroll viewport this component owns (height + overflow).
  // The flight ACARS panel passes its `max-h-[480px] overflow-y-auto`; the feed
  // gets a viewport-relative default.
  scrollClassName?: string;
  // Optional testid on the scroll viewport (the panel uses `*-acars-scroll`).
  scrollTestId?: string;
}

// Known label codes get a tooltip with the human-readable name; tabIndex makes
// the badge a focusable trigger (Radix opens on hover OR focus — focus is also
// the keyboard-a11y path). Unknown codes stay a bare badge.
function LabelBadge({ code }: { code: string }) {
  const name = labelName(code);
  if (!name) return <Badge variant="muted">{code}</Badge>;
  return (
    <SimpleTooltip content={name}>
      <Badge variant="muted" tabIndex={0}>
        {code}
      </Badge>
    </SimpleTooltip>
  );
}

function FiledRoute({ route }: { route: NonNullable<Vdl2Message['filed_route']> }) {
  return (
    <div className="mt-1 text-xs text-[var(--color-text-dim)]" data-testid="vdl2-filed-route">
      <span className="text-[var(--color-text)]">Filed route</span> — {route.dep} → {route.arr}
      {route.company_route && <> · via {route.company_route}</>}
      {route.sid && <> · SID {route.sid}</>}
      {route.star && <> · STAR {route.star}</>}
      {route.approach && <> · APP {route.approach}</>}
    </div>
  );
}

export function MessageList({
  messages,
  onHexClick,
  onRegClick,
  decode: decodeProp,
  scrollClassName = 'max-h-[75vh] overflow-y-auto',
  scrollTestId,
}: Props) {
  // Reactive timestamp formatter — re-renders when the 12h/24h clock-format
  // store toggles (a bare `fmtTs` import would snapshot the format at mount).
  const { fmtTs } = useFormat();
  const hookDecode = useAcarsDecoder();
  const decode = decodeProp ?? hookDecode;

  const parentRef = useRef<HTMLDivElement>(null);
  // react-virtual's API returns functions React Compiler can't memoize, so it
  // skips compiling this component (intentional — the virtualizer manages its
  // own re-render cadence). Silence the heads-up rule for this known case.
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirt = useVirtualizer({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 96, // corrected per-row by measureElement
    overscan: 8,
  });

  return (
    <div ref={parentRef} className={scrollClassName} data-testid={scrollTestId}>
      <ul className="relative" data-testid="vdl2-list" style={{ height: rowVirt.getTotalSize() }}>
        {rowVirt.getVirtualItems().map((vrow) => {
          const m = messages[vrow.index];
          const decoded = decode && m.body ? decode(m) : null;
          // Body-category chip — only when the row has no richer rendering already
          // (airframes decoded line or a filed_route line).
          const kind = !decoded && !m.filed_route ? bodyKind(m.body, m.label) : null;
          return (
            <li
              key={m.id}
              ref={rowVirt.measureElement}
              data-index={vrow.index}
              className="absolute left-0 top-0 w-full border-b border-[var(--color-border-default)] py-2"
              style={{ transform: `translateY(${vrow.start}px)` }}
              data-testid="vdl2-message-row"
            >
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
                {m.label && <LabelBadge code={m.label} />}
                {kind && (
                  <span
                    className="rounded border border-[var(--color-border-default)] px-1.5 py-0.5 text-[11px] text-[var(--color-text-dim)]"
                    data-testid="vdl2-kind"
                  >
                    {kind}
                  </span>
                )}
                {m.dsta && <span className="text-[var(--color-text-dim)]">→ {m.dsta}</span>}
                {decoded && (
                  <span
                    className="font-semibold text-[var(--color-accent)]"
                    data-testid="vdl2-decoded-desc"
                  >
                    {decoded.description}
                  </span>
                )}
              </div>
              {decoded && decoded.items.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1.5" data-testid="vdl2-decoded">
                  {decoded.items.map((it, i) => (
                    <span
                      key={i}
                      className="rounded border border-[var(--color-border-default)] px-1.5 py-0.5 text-[11px]"
                    >
                      <span className="text-[var(--color-text-dim)]">{it.label}</span>{' '}
                      <span className="text-[var(--color-text)]">{it.value}</span>
                    </span>
                  ))}
                </div>
              )}
              {m.filed_route && <FiledRoute route={m.filed_route} />}
              {m.body && (
                <pre
                  className={`mt-1 whitespace-pre-wrap break-all font-mono text-xs ${
                    decoded ? 'text-[var(--color-text-dim)]' : 'text-[var(--color-text)]'
                  }`}
                >
                  {m.body}
                </pre>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
