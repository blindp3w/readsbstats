import { useEffect, useRef } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useFormat } from '@/hooks/useFormat';
import { Badge } from '@/components/ui/Badge';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import { labelName } from '@/lib/vdl2Labels';
import { bodyKind } from '@/lib/vdl2Kinds';
import { useAcarsDecoder } from '@/hooks/useAcarsDecoder';
import type { DecodedAcars } from '@/lib/acarsDecode';
import { structureItems, type Segment } from '@/lib/acarsStructured';
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
// `decode` defaults to useAcarsDecoder() (lazy airframes decoder + local fallbacks). Tests inject
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
  // Reports the scroll viewport's scrollTop on every scroll (and 0 on unmount) so
  // the feed can pause live polling while the user is reading further down.
  onScrollTopChange?: (top: number) => void;
  // Optional ref to the scroll viewport, so the parent can scroll it back to top.
  viewportRef?: React.RefObject<HTMLDivElement | null>;
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

const CHIP_BOX =
  'min-w-0 max-w-full rounded border border-[var(--color-border-default)] px-1.5 py-0.5 text-[11px]';

// One decoded-items segment (see lib/acarsStructured). Unknown/unparseable items
// arrive as `chip` and render exactly as before. All text is escaped React children.
function SegmentView({ seg }: { seg: Segment }) {
  switch (seg.kind) {
    case 'position':
      return (
        <span className={CHIP_BOX} data-testid="vdl2-pos-chip" title={seg.label}>
          <span aria-hidden="true">📍 </span>
          <span className="sr-only">{seg.label} </span>
          <span className="tabnum font-mono text-[var(--color-text)]">{seg.text}</span>
        </span>
      );
    case 'figure':
      return (
        <span className={CHIP_BOX} data-testid="vdl2-figure">
          <span className="text-[var(--color-text-dim)]">{seg.label}</span>{' '}
          <span className="tabnum font-semibold text-[var(--color-text)]">{seg.value}</span>
        </span>
      );
    case 'route':
      return (
        <div className={`${CHIP_BOX} basis-full`} data-testid="vdl2-route">
          <span className="text-[var(--color-text-dim)]">{seg.label}</span>
          {seg.name && <span className="font-mono text-[var(--color-text)]"> {seg.name}</span>}
          <ol className="mt-0.5 flex flex-wrap items-baseline gap-x-1 gap-y-0.5">
            {seg.waypoints.map((w, i) => (
              <li key={i} className="flex items-baseline gap-1 break-all">
                {i > 0 && (
                  <span className="text-[var(--color-text-dim)]" aria-hidden="true">
                    ›
                  </span>
                )}
                <span className="font-mono text-[var(--color-text)]">{w.name}</span>
                {w.detail && <span className="text-[10px] text-[var(--color-text-dim)]">{w.detail}</span>}
              </li>
            ))}
            {seg.more > 0 && <li className="text-[var(--color-text-dim)]">+{seg.more} more</li>}
          </ol>
        </div>
      );
    case 'landing':
      return (
        <div className="min-w-0 max-w-full basis-full overflow-x-auto" data-testid="vdl2-landing">
          <table className="w-full border-collapse text-[11px]">
            <caption className="sr-only">Landing runway data</caption>
            <thead>
              <tr className="text-left text-[var(--color-text-dim)]">
                <th scope="col" className="pr-3 font-normal">
                  Runway
                </th>
                <th scope="col" className="font-normal">
                  Data
                </th>
              </tr>
            </thead>
            <tbody>
              {seg.rows.map((r, i) => (
                <tr key={i} className="border-t border-[var(--color-border-default)] align-top">
                  <th scope="row" className="whitespace-nowrap pr-3 text-left font-mono font-normal">
                    {r.runway}
                  </th>
                  <td className="break-words">
                    {r.parts.map((p, j) => (
                      <span key={j} className="mr-2 inline-block">
                        <span className="text-[var(--color-text-dim)]">{p.label}</span>{' '}
                        <span className="tabnum text-[var(--color-text)]">{p.value}</span>
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    default:
      return (
        <span className={`${CHIP_BOX} whitespace-pre-wrap break-all`}>
          <span className="text-[var(--color-text-dim)]">{seg.item.label}</span>{' '}
          <span className="text-[var(--color-text)]">{seg.item.value}</span>
        </span>
      );
  }
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
  onScrollTopChange,
  viewportRef,
}: Props) {
  // Reactive timestamp formatter — re-renders when the 12h/24h clock-format
  // store toggles (a bare `fmtTs` import would snapshot the format at mount).
  const { fmtTs } = useFormat();
  const hookDecode = useAcarsDecoder();
  const decode = decodeProp ?? hookDecode;

  const ownRef = useRef<HTMLDivElement>(null);
  const parentRef = viewportRef ?? ownRef;

  // Latest callback in a ref so the unmount cleanup below runs once, not on every
  // render of a parent that passes an inline function.
  const onTopRef = useRef(onScrollTopChange);
  useEffect(() => {
    onTopRef.current = onScrollTopChange;
  });
  useEffect(
    () => () => {
      onTopRef.current?.(0);
    },
    [],
  );
  // react-virtual's API returns functions React Compiler can't memoize, so it
  // skips compiling this component (intentional — the virtualizer manages its
  // own re-render cadence). Silence the heads-up rule for this known case.
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirt = useVirtualizer({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 96, // corrected per-row by measureElement
    overscan: 8,
    // Key the measured-height cache by message id, not the default array index.
    // The feed live-refetches its newest page every 15 s (ORDER BY id DESC), so
    // a new message shifts every row to index+1; an index-keyed cache would then
    // apply stale heights to the wrong messages until each re-measures.
    getItemKey: (index) => messages[index].id,
  });

  return (
    <div
      ref={parentRef}
      className={scrollClassName}
      data-testid={scrollTestId}
      onScroll={onScrollTopChange ? (e) => onScrollTopChange(e.currentTarget.scrollTop) : undefined}
    >
      <ul className="relative" data-testid="vdl2-list" style={{ height: rowVirt.getTotalSize() }}>
        {rowVirt.getVirtualItems().map((vrow) => {
          const m = messages[vrow.index];
          const decoded = m.body ? decode(m) : null;
          // Body-category chip — only when the row has no richer rendering already
          // (airframes decoded line or a filed_route line).
          const kind = !decoded && !m.filed_route ? bodyKind(m.body, m.label, m.sublabel) : null;
          return (
            <li
              key={m.id}
              ref={rowVirt.measureElement}
              data-index={vrow.index}
              className="absolute left-0 top-0 w-full min-w-0 overflow-hidden border-b border-[var(--color-border-default)] py-2"
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
                      aria-label={`Filter by hex ${m.icao_hex}`}
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
                      aria-label={`Filter by registration ${m.registration}`}
                      onClick={() => onRegClick(m.registration!)}
                    >
                      {m.registration}
                    </button>
                  ) : (
                    <span className="font-mono">{m.registration}</span>
                  ))}
                {m.flight && <span className="font-mono">{m.flight}</span>}
                {m.label && <LabelBadge code={m.label} />}
                {m.more === 1 && (
                  <Badge
                    variant="muted"
                    title="Intermediate block of a multi-block message"
                    data-testid="vdl2-row-partial"
                  >
                    partial
                    {/* title doesn't work on touch: keep a screen-reader text equivalent */}
                    <span className="sr-only"> (intermediate block of a multi-block message)</span>
                  </Badge>
                )}
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
                <div className="mt-1 flex min-w-0 flex-wrap gap-1.5" data-testid="vdl2-decoded">
                  {structureItems(decoded).map((seg, i) => (
                    <SegmentView key={i} seg={seg} />
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
