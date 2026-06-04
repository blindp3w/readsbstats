import { useEffect, useState } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { fmtTs } from '@/lib/format';
import { useSearchParam, useSearchParamBatch } from '@/hooks/useSearchParam';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';

// VDL2 / ACARS message feed (opt-in feature; the nav item + this page only
// appear when RSBS_VDL2_ENABLED). Data comes from the SEPARATE vdl2.db via
// /api/vdl2/*. Message bodies are untrusted upstream text — always rendered as
// plain React text children, which React escapes automatically (no raw HTML).

interface Vdl2Message {
  id: number;
  ts: number;
  icao_hex: string | null;
  registration: string | null;
  flight: string | null;
  label: string | null;
  freq: number | null;
  dsta: string | null;
  body: string | null;
  decoder: string | null;
}

interface Vdl2MessagesResponse {
  messages: Vdl2Message[];
  next_before_id: number | null;
}

interface Vdl2Stats {
  total: number;
  last_hour: number;
  aircraft: number;
}

const PAGE = 100;

export default function Vdl2Page() {
  const settingsQ = useQuery({
    queryKey: ['settings'],
    queryFn: () => apiJson<{ vdl2_enabled?: boolean }>('settings'),
    staleTime: 60_000,
  });
  const enabled = settingsQ.data?.vdl2_enabled === true;

  // Filters live in the URL so the view is shareable + back-button friendly.
  const [q, setQ] = useSearchParam('q', '');
  const [label, setLabel] = useSearchParam('label', '');
  const [reg, setReg] = useSearchParam('reg', '');
  const [hex, setHex] = useSearchParam('hex', '');

  const updateFilters = useSearchParamBatch();

  // Debounce the search box into the `q` URL param (one update per pause).
  const [qInput, setQInput] = useState(q);
  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(t);
  }, [qInput, setQ]);
  // Keep the box in sync when `q` changes externally (back/forward, Clear).
  useEffect(() => {
    setQInput(q);
  }, [q]);

  const feed = useInfiniteQuery({
    queryKey: ['vdl2-messages', { q, label, reg, hex }],
    enabled,
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => {
      const p = new URLSearchParams({ limit: String(PAGE) });
      if (pageParam) p.set('before_id', String(pageParam));
      if (q) p.set('q', q);
      if (label) p.set('label', label);
      if (reg) p.set('reg', reg);
      if (hex) p.set('hex', hex);
      return apiJson<Vdl2MessagesResponse>('vdl2/messages?' + p.toString());
    },
    getNextPageParam: (last) => last.next_before_id ?? undefined,
    // Live-refresh only the initial page; once the user pages into history
    // (more than one page loaded) stop polling so we don't refetch everything.
    refetchInterval: (query) => ((query.state.data?.pages.length ?? 0) > 1 ? false : 15_000),
    staleTime: 5_000,
  });

  const stats = useQuery({
    queryKey: ['vdl2-stats'],
    queryFn: () => apiJson<Vdl2Stats>('vdl2/stats'),
    enabled,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });

  if (settingsQ.isSuccess && !enabled) {
    return (
      <div className="mx-auto max-w-5xl px-4 py-6" data-testid="page-vdl2">
        <Alert variant="info" data-testid="vdl2-disabled">
          The VDL2 / ACARS feature is disabled. Set <code>RSBS_VDL2_ENABLED=true</code> on the web
          and ingest services to enable it.
        </Alert>
      </div>
    );
  }

  const messages = feed.data?.pages.flatMap((p) => p.messages) ?? [];
  const hasActiveFilter = Boolean(q || label || reg || hex);

  function clearFilters() {
    // One atomic update — React Router v7 drops all but the last of N
    // back-to-back setSearchParams calls in a single handler (see
    // useSearchParam.ts). The qInput box re-syncs via the effect on `q`.
    setQInput('');
    updateFilters({ q: null, label: null, reg: null, hex: null });
  }

  return (
    <div className="mx-auto max-w-5xl space-y-4 px-4 py-6" data-testid="page-vdl2">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">VDL2 / ACARS Messages</h1>
        <p className="text-sm text-[var(--color-text-dim)]">
          Live VHF Data Link Mode 2 traffic decoded by the receiver. Stored separately from flight
          history.
        </p>
      </header>

      {/* Stats */}
      <div className="flex flex-wrap gap-2" data-testid="vdl2-stats">
        <Badge variant="muted" data-testid="vdl2-stat-total">
          {stats.data ? stats.data.total.toLocaleString() : '—'} total
        </Badge>
        <Badge variant="muted">
          {stats.data ? stats.data.last_hour.toLocaleString() : '—'} last hour
        </Badge>
        <Badge variant="muted">
          {stats.data ? stats.data.aircraft.toLocaleString() : '—'} aircraft
        </Badge>
      </div>

      {/* Filters */}
      <Card data-testid="vdl2-filters-card">
        <CardContent className="pt-3 pb-3">
          <div className="flex flex-wrap items-end gap-2 md:flex-nowrap">
            <div className="min-w-[200px] flex-1">
              <Label htmlFor="vdl2-search">Search text</Label>
              <Input
                id="vdl2-search"
                type="text"
                value={qInput}
                onChange={(e) => setQInput(e.target.value)}
                placeholder="e.g. EPWA, gate, callsign…"
                autoComplete="off"
                spellCheck={false}
                data-testid="vdl2-search"
              />
            </div>
            <div className="w-[120px] shrink-0">
              <Label htmlFor="vdl2-label">Label</Label>
              <Input
                id="vdl2-label"
                type="text"
                value={label}
                onChange={(e) => setLabel(e.target.value.trim())}
                placeholder="H1"
                autoComplete="off"
                spellCheck={false}
                data-testid="vdl2-label"
              />
            </div>
            <div className="w-[150px] shrink-0">
              <Label htmlFor="vdl2-reg">Registration</Label>
              <Input
                id="vdl2-reg"
                type="text"
                value={reg}
                onChange={(e) => setReg(e.target.value.trim())}
                placeholder="SP-"
                autoComplete="off"
                spellCheck={false}
                data-testid="vdl2-reg"
              />
            </div>
            {hex && (
              <Badge variant="default" data-testid="vdl2-hex-chip">
                hex: {hex}
              </Badge>
            )}
            {hasActiveFilter && (
              <Button
                variant="secondary"
                size="field"
                onClick={clearFilters}
                data-testid="vdl2-clear"
                className="shrink-0 self-end"
              >
                Clear
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Feed */}
      <Card data-testid="vdl2-feed-card">
        <CardHeader>
          <CardTitle>Messages</CardTitle>
        </CardHeader>
        <CardContent>
          {feed.isError && (
            <Alert variant="error">Failed to load: {(feed.error as Error).message}</Alert>
          )}
          {feed.isLoading && <Skeleton className="h-40 w-full" />}
          {feed.isSuccess && messages.length === 0 && (
            <p
              className="py-6 text-center text-sm text-[var(--color-text-dim)]"
              data-testid="vdl2-empty"
            >
              No messages{hasActiveFilter ? ' match the current filters' : ' received yet'}.
            </p>
          )}
          {messages.length > 0 && (
            <ul className="divide-y divide-[var(--color-border-default)]" data-testid="vdl2-list">
              {messages.map((m) => (
                <li key={m.id} className="py-2" data-testid="vdl2-message-row">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                    <span className="tabnum text-[var(--color-text-dim)]">{fmtTs(m.ts)}</span>
                    {m.icao_hex && (
                      <button
                        type="button"
                        className="font-mono text-[var(--color-accent)] hover:underline"
                        onClick={() => setHex(m.icao_hex!)}
                        data-testid="vdl2-row-hex"
                      >
                        {m.icao_hex}
                      </button>
                    )}
                    {m.registration && (
                      <button
                        type="button"
                        className="font-mono hover:underline"
                        onClick={() => setReg(m.registration!)}
                      >
                        {m.registration}
                      </button>
                    )}
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
          )}
          {feed.hasNextPage && (
            <div className="mt-3 text-center">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => feed.fetchNextPage()}
                disabled={feed.isFetchingNextPage}
                data-testid="vdl2-load-older"
              >
                {feed.isFetchingNextPage ? 'Loading…' : 'Load older'}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
