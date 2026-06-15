import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeftIcon, CheckIcon } from '@radix-ui/react-icons';
import { toast } from 'sonner';
import { apiFetch, apiJson } from '@/lib/api';
import { SimpleTooltip } from '@/components/ui/Tooltip';
import type { WatchlistEntry } from '@/lib/types';
import { useSearchParam, useSearchParamBatch } from '@/hooks/useSearchParam';
import { safeUrl } from '@/lib/safeUrl';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { FlagBadge } from '@/components/FlagBadge';
import { PhotoLightbox } from '@/components/PhotoLightbox';
import { FlightsTable, type Flight, type SortKey, type SortDir } from '@/components/FlightsTable';
import { Pagination } from '@/components/Pagination';
import { AcarsPanel } from '@/components/vdl2/AcarsPanel';
import { fmtDur } from '@/lib/format';
import { useFormat } from '@/hooks/useFormat';

interface PhotoResp {
  thumbnail_url: string | null;
  large_url: string | null;
  link_url: string | null;
  photographer: string | null;
  is_type_photo: boolean;
}

interface AircraftFlightsResp {
  total: number;
  icao_hex: string;
  aircraft_info: {
    registration?: string | null;
    type_code?: string | null;
    type_desc?: string | null;
    flags?: number;
    first_seen?: number;
    last_seen?: number;
    total_duration_sec?: number;
    country?: string | null;
  };
  flights: Flight[];
}

const PAGE_SIZE = 100;
const SORT_KEYS: SortKey[] = [
  'first_seen',
  'duration_sec',
  'callsign',
  'aircraft_type',
  'max_alt_baro',
  'max_gs',
  'max_distance_nm',
  'total_positions',
  'origin_icao',
];
function isSortKey(s: string): s is SortKey {
  return (SORT_KEYS as string[]).includes(s);
}

export default function AircraftPage() {
  const { icao: rawIcao } = useParams<{ icao: string }>();
  const icao = (rawIcao ?? '').toLowerCase().replace(/^~/, '');

  const [sortByRaw] = useSearchParam('sort_by', 'first_seen');
  const [sortDirRaw] = useSearchParam('sort_dir', 'desc');
  const [offset, setOffset] = useSearchParam('offset', 0);
  const update = useSearchParamBatch();
  const sortBy: SortKey = isSortKey(String(sortByRaw)) ? (sortByRaw as SortKey) : 'first_seen';
  const sortDir: SortDir = String(sortDirRaw) === 'asc' ? 'asc' : 'desc';
  const { fmtTs } = useFormat();

  const qs = new URLSearchParams();
  qs.set('sort_by', sortBy);
  qs.set('sort_dir', sortDir);
  qs.set('limit', String(PAGE_SIZE));
  qs.set('offset', String(offset));

  const flightsQ = useQuery<AircraftFlightsResp>({
    queryKey: ['aircraft-flights', icao, qs.toString()],
    queryFn: () => apiJson<AircraftFlightsResp>(`aircraft/${icao}/flights?${qs.toString()}`),
    enabled: !!icao,
    placeholderData: (prev) => prev,
  });

  const photoQ = useQuery<PhotoResp | null>({
    queryKey: ['aircraft-photo', icao],
    queryFn: () => apiJson<PhotoResp | null>(`aircraft/${icao}/photo`),
    enabled: !!icao,
    staleTime: 600_000, // photos are cached server-side for 30 days
  });

  const info = flightsQ.data?.aircraft_info ?? {};

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6" data-testid="page-aircraft">
      <header>
        <Link
          to="/history"
          className="inline-flex items-center gap-1 text-xs text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
        >
          <ArrowLeftIcon aria-hidden="true" />
          back to history
        </Link>
        <h1 className="mt-1 text-xl font-semibold font-mono tabnum">{icao}</h1>
      </header>

      <Card data-testid="aircraft-info-card">
        <CardHeader className="flex flex-row items-center justify-between gap-3">
          <CardTitle>
            <span className="flex flex-wrap items-center gap-2">
              {info.registration || icao}
              {typeof info.flags === 'number' ? <FlagBadge flags={info.flags} /> : null}
              {info.country ? <Badge variant="muted">{info.country}</Badge> : null}
            </span>
          </CardTitle>
          <WatchButton icao={icao} />
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-[200px_1fr]">
            <PhotoBox q={photoQ} label={info.registration || icao || 'Aircraft'} />
            <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
              <dt className="text-[var(--color-text-dim)]">Type</dt>
              <dd>
                {info.type_code ?? '—'}
                {info.type_desc ? (
                  <span className="ml-1 text-[var(--color-text-dim)]">· {info.type_desc}</span>
                ) : null}
              </dd>
              <dt className="text-[var(--color-text-dim)]">Flights</dt>
              <dd className="tabnum">{flightsQ.data?.total?.toLocaleString() ?? '—'}</dd>
              <dt className="text-[var(--color-text-dim)]">First seen</dt>
              <dd className="tabnum">{fmtTs(info.first_seen ?? null)}</dd>
              <dt className="text-[var(--color-text-dim)]">Last seen</dt>
              <dd className="tabnum">{fmtTs(info.last_seen ?? null)}</dd>
              <dt className="text-[var(--color-text-dim)]">Time tracked</dt>
              <dd className="tabnum">{fmtDur(info.total_duration_sec ?? null)}</dd>
            </dl>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Flights</CardTitle>
        </CardHeader>
        <CardContent>
          <FlightsTable
            flights={flightsQ.data?.flights}
            isLoading={flightsQ.isLoading}
            error={flightsQ.isError ? (flightsQ.error as Error) : null}
            sortBy={sortBy}
            sortDir={sortDir}
            onSortChange={(k, d) => {
              update({ sort_by: k, sort_dir: d, offset: null });
            }}
          />
          {flightsQ.data && flightsQ.data.total > PAGE_SIZE && (
            <Pagination
              total={flightsQ.data.total}
              limit={PAGE_SIZE}
              offset={offset}
              onOffsetChange={setOffset}
            />
          )}
        </CardContent>
      </Card>

      {/* ACARS across the airframe's whole tracked history. Self-gates on
          RSBS_VDL2_ENABLED; only mounts once the first/last-seen window loads. */}
      {icao && info.first_seen != null && info.last_seen != null && (
        <AcarsPanel
          icao={icao}
          firstSeen={info.first_seen}
          lastSeen={info.last_seen}
          context="aircraft"
        />
      )}
    </div>
  );
}

// Watchlist add/remove for the current aircraft. Resolves "am I watching this
// already?" by reading the existing /api/watchlist query (shared with the
// Watchlist page), then offers Add or Remove accordingly.
// `WatchlistEntry` is declared once in @/lib/types and shared with Watchlist.tsx.

function WatchButton({ icao }: { icao: string }) {
  const qc = useQueryClient();
  const listQ = useQuery<{ entries: WatchlistEntry[] }>({
    queryKey: ['watchlist'],
    queryFn: () => apiJson<{ entries: WatchlistEntry[] }>('watchlist'),
    staleTime: 30_000,
  });

  // `icao` is already lowercased + `~`-stripped (see AircraftPage). Normalize the
  // stored watchlist value the SAME way before comparing, so an anonymous
  // airframe saved as `~ABC123` still matches the stripped `abc123` (BUG-10).
  const existing = listQ.data?.entries.find(
    (e) => e.match_type === 'icao' && e.value.toLowerCase().replace(/^~/, '') === icao,
  );

  const addMut = useMutation({
    mutationFn: async () =>
      apiJson<WatchlistEntry>('watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ match_type: 'icao', value: icao }),
      }),
    onSuccess: () => {
      toast.success(`Added ${icao} to watchlist`);
      qc.invalidateQueries({ queryKey: ['watchlist'] });
    },
    onError: (err: Error) => {
      toast.error(err.message);
    },
  });

  const removeMut = useMutation({
    mutationFn: async (id: number) => {
      await apiFetch(`watchlist/${id}`, { method: 'DELETE' });
    },
    onSuccess: () => {
      toast.success(`Removed ${icao} from watchlist`);
      qc.invalidateQueries({ queryKey: ['watchlist'] });
    },
    onError: (err: Error) => {
      toast.error(err.message);
    },
  });

  if (!icao) return null;
  if (listQ.isLoading) return null;

  if (existing) {
    return (
      <SimpleTooltip content="Click to remove from watchlist">
        <Button
          size="sm"
          variant="secondary"
          onClick={() => removeMut.mutate(existing.id)}
          disabled={removeMut.isPending}
          data-testid="aircraft-watch-toggle"
          aria-pressed={true}
        >
          <CheckIcon aria-hidden="true" />
          Watching
        </Button>
      </SimpleTooltip>
    );
  }

  return (
    <Button
      size="sm"
      onClick={() => addMut.mutate()}
      disabled={addMut.isPending}
      data-testid="aircraft-watch-toggle"
      aria-pressed={false}
    >
      + Watch
    </Button>
  );
}

function PhotoBox({
  q,
  label,
}: {
  q: { data: PhotoResp | null | undefined; isLoading: boolean };
  // Aircraft label (registration or icao_hex) — used as the lightbox's
  // accessible name (via <Dlg.Title> sr-only) and the enlarged image's
  // alt text. Radix requires a non-empty title for the dialog to satisfy
  // its accessible-name contract.
  label: string;
}) {
  if (q.isLoading) return <Skeleton className="aspect-[4/3] w-full" />;
  if (!q.data) return null;
  const url = safeUrl(q.data.large_url) || safeUrl(q.data.thumbnail_url);
  if (!url) {
    return (
      <div className="flex aspect-[4/3] items-center justify-center rounded bg-[var(--color-surface-2)] text-xs text-[var(--color-text-dim)]">
        no photo
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <PhotoLightbox photo={q.data} alt={label}>
        <button
          type="button"
          aria-label="Enlarge photo"
          data-testid="aircraft-photo-trigger"
          className="block aspect-[4/3] w-full overflow-hidden rounded bg-[var(--color-surface-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
        >
          <img src={url} alt={label} loading="lazy" className="h-full w-full object-cover" />
        </button>
      </PhotoLightbox>
      {q.data.photographer && (
        <p className="text-xs text-[var(--color-text-dim)]">
          © {q.data.photographer}
          {q.data.is_type_photo ? ' (type photo)' : ''}
        </p>
      )}
    </div>
  );
}
