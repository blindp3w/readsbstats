import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { MixerHorizontalIcon, CheckIcon } from '@radix-ui/react-icons';
import { apiJson } from '@/lib/api';
import { useSearchParam, useSearchParamBatch } from '@/hooks/useSearchParam';
import { safeUrl } from '@/lib/safeUrl';
import { Card, CardContent } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';
import { FlagBadge } from '@/components/FlagBadge';
import { Pagination } from '@/components/Pagination';
import { ToggleGroupRoot, ToggleGroupItem } from '@/components/ui/ToggleGroup';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/Popover';
import { fmtTs } from '@/lib/format';
import { cn } from '@/lib/cn';

interface FlaggedAircraft {
  icao_hex: string;
  registration: string | null;
  aircraft_type: string | null;
  type_desc: string | null;
  flags: number;
  flight_count: number;
  first_seen: number;
  last_seen: number;
  thumbnail_url: string | null;
  large_url: string | null;
  link_url: string | null;
  photographer: string | null;
  is_type_photo: boolean;
  country: string | null;
}

interface FlaggedResponse {
  total: number;
  aircraft: FlaggedAircraft[];
}

const PAGE_SIZE = 60;

const FILTER_OPTIONS = [
  { value: '', label: 'All' },
  { value: 'military', label: 'Military' },
  { value: 'interesting', label: 'Interesting' },
  { value: 'anonymous', label: 'Anonymous' },
];

const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: 'last_seen', label: 'Last seen' },
  { value: 'first_seen', label: 'First seen' },
  { value: 'flight_count', label: 'Flight count' },
  { value: 'registration', label: 'Registration' },
  { value: 'aircraft_type', label: 'Type' },
];

export default function GalleryPage() {
  // Read-only: we always WRITE via the batch helper so multi-param updates
  // (e.g. flag change resets offset) commit atomically. Reading per-param is
  // fine because it's just URLSearchParams.get().
  const [filter] = useSearchParam('flags', '');
  const [sort] = useSearchParam('sort_by', 'last_seen');
  const [offset] = useSearchParam('offset', 0);
  const update = useSearchParamBatch();

  const qs = new URLSearchParams();
  if (filter) qs.set('flags', filter);
  qs.set('sort_by', sort);
  qs.set('limit', String(PAGE_SIZE));
  qs.set('offset', String(offset));
  const q = useQuery<FlaggedResponse>({
    queryKey: ['aircraft-flagged', qs.toString()],
    queryFn: () => apiJson<FlaggedResponse>(`aircraft/flagged?${qs.toString()}`),
    placeholderData: (prev) => prev,
  });

  const currentSortLabel =
    SORT_OPTIONS.find((o) => o.value === sort)?.label ?? SORT_OPTIONS[0].label;

  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-6" data-testid="page-gallery">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">Gallery</h1>
          <p className="text-xs text-[var(--color-text-dim)]">
            Flagged aircraft — military, interesting, anonymous (non-ICAO hex).
            {q.data ? ` ${q.data.total.toLocaleString()} matching.` : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ToggleGroupRoot
            type="single"
            value={String(filter) || 'all'}
            onValueChange={(v) => {
              update({ flags: v === 'all' ? null : v, offset: 0 });
            }}
            aria-label="Filter by flag"
            data-testid="gallery-filter-group"
            className="flex-nowrap"
          >
            {FILTER_OPTIONS.map((opt) => (
              <ToggleGroupItem
                key={opt.value || 'all'}
                value={opt.value || 'all'}
                data-testid={`gallery-filter-${opt.value || 'all'}`}
              >
                {opt.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroupRoot>
          <SortPopover
            value={String(sort)}
            currentLabel={currentSortLabel}
            onChange={(v) => update({ sort_by: v === 'last_seen' ? null : v, offset: 0 })}
          />
        </div>
      </header>

      {q.isError && <Alert variant="error">Failed to load: {(q.error as Error).message}</Alert>}

      {q.isLoading && (
        <div
          className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4"
          data-testid="gallery-skeleton-grid"
        >
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-56 w-full" />
          ))}
        </div>
      )}

      {q.data && q.data.aircraft.length === 0 && (
        <Alert variant="info" data-testid="gallery-empty">
          No matching aircraft.
        </Alert>
      )}

      {q.data && q.data.aircraft.length > 0 && (
        <div
          className="grid gap-3 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4"
          data-testid="gallery-grid"
        >
          {q.data.aircraft.map((a) => (
            <Card
              key={a.icao_hex}
              className="overflow-hidden"
              data-testid={`gallery-card-${a.icao_hex}`}
            >
              <Link
                to={`/aircraft/${a.icao_hex}`}
                className="block focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
              >
                <PhotoBox photo={a} />
                <CardContent className="space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-medium tabnum">
                      {a.registration || a.icao_hex}
                    </div>
                    <FlagBadge flags={a.flags} />
                  </div>
                  <div className="text-xs text-[var(--color-text-dim)]">
                    {a.aircraft_type ?? '—'} · {a.type_desc || a.country || ''}
                  </div>
                  <div className="text-xs text-[var(--color-text-dim)] tabnum">
                    {a.flight_count} flights · last {fmtTs(a.last_seen)}
                  </div>
                  {a.is_type_photo && (
                    <Badge variant="muted" className="mt-1">
                      Type photo
                    </Badge>
                  )}
                </CardContent>
              </Link>
            </Card>
          ))}
        </div>
      )}

      {q.data && q.data.total > PAGE_SIZE && (
        <Pagination
          total={q.data.total}
          limit={PAGE_SIZE}
          offset={Number(offset)}
          onOffsetChange={(o) => update({ offset: o })}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SortPopover — icon button + Popover with a radio list. Mirrors the visual
// weight of the Custom-range button on /v2/ so the controls line up at the
// same height.
// ---------------------------------------------------------------------------

const SortIcon = MixerHorizontalIcon;

function SortPopover({
  value,
  currentLabel,
  onChange,
}: {
  value: string;
  currentLabel: string;
  onChange: (next: string) => void;
}) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Sort: ${currentLabel}`}
          title={`Sort: ${currentLabel}`}
          data-testid="gallery-sort"
          className={cn(
            'inline-flex items-center gap-1.5 rounded border px-3 text-xs font-medium transition-colors',
            'border-[var(--color-border-default)] text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
            'min-h-[40px] min-w-[44px] md:min-h-[32px]',
          )}
        >
          <SortIcon />
          <span className="hidden sm:inline">{currentLabel}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-[180px] p-1" data-testid="gallery-sort-panel">
        <ul role="radiogroup" aria-label="Sort options" className="space-y-0.5">
          {SORT_OPTIONS.map((opt) => {
            const selected = opt.value === value;
            return (
              <li key={opt.value}>
                <button
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  onClick={() => onChange(opt.value)}
                  data-testid={`gallery-sort-${opt.value}`}
                  className={cn(
                    'flex w-full items-center justify-between rounded px-2.5 py-1.5 text-xs',
                    'hover:bg-[var(--color-surface-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
                    selected
                      ? 'text-[var(--color-accent)] font-medium'
                      : 'text-[var(--color-text)]',
                  )}
                >
                  <span>{opt.label}</span>
                  {selected ? <CheckIcon aria-hidden="true" /> : null}
                </button>
              </li>
            );
          })}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

function PhotoBox({ photo }: { photo: FlaggedAircraft }) {
  const src = safeUrl(photo.thumbnail_url) || safeUrl(photo.large_url);
  if (!src) {
    return (
      <div className="flex aspect-[4/3] items-center justify-center bg-[var(--color-surface-2)] text-xs text-[var(--color-text-dim)]">
        no photo
      </div>
    );
  }
  return (
    <div className="aspect-[4/3] overflow-hidden bg-[var(--color-surface-2)]">
      <img
        src={src}
        alt={photo.registration ?? photo.icao_hex}
        loading="lazy"
        className="h-full w-full object-cover"
      />
    </div>
  );
}
