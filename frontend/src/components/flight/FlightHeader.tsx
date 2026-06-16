// Flight detail compact header (M3.1): photo + identity + key stats.
// Extracted verbatim from pages/Flight.tsx.

import { safeUrl } from '@/lib/safeUrl';
import { Card, CardContent } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Badge } from '@/components/ui/Badge';
import { FlagBadge, SourceBadge } from '@/components/FlagBadge';
import { useFormat } from '@/hooks/useFormat';
import { fmtDur } from '@/lib/format';
import { MetricCell } from '@/components/flight/MetricCell';
import { useVdl2FlightMessages } from '@/hooks/useVdl2Enabled';
import { haversineNm, bearingFromReceiver } from '@/lib/geo';
import { PhotoLightbox } from '@/components/PhotoLightbox';
import { cn } from '@/lib/cn';
import { Link } from 'react-router-dom';
import type { AtMax, FlightDetail, PhotoResp, Position } from '@/components/flight/types';

// ---------------------------------------------------------------------------
// Header: photo + key stats
// ---------------------------------------------------------------------------

// At-max position lookups for the M3.1 header sub-labels. Computed
// client-side from `positions` (NOT via equality against the flight-level
// aggregates) because `max_gs` is REAL and `max_distance_nm` requires
// per-position haversine. Single pass, O(n).
function computeAtMax(positions: Position[], recLat: number | null, recLon: number | null): AtMax {
  if (positions.length === 0) return { altRate: null, speedTrack: null, distBearing: null };
  let maxAltIdx = -1;
  let maxAlt = -Infinity;
  let maxGsIdx = -1;
  let maxGs = -Infinity;
  // Track the farthest fix's coords directly (not its index) so the bearing
  // computation needs no non-null assertion — these are only ever set inside
  // the lat/lon != null guard below.
  let maxDistLat: number | null = null;
  let maxDistLon: number | null = null;
  let maxDist = -Infinity;
  for (let i = 0; i < positions.length; i++) {
    const p = positions[i];
    if (p.alt_baro != null && p.alt_baro > maxAlt) {
      maxAlt = p.alt_baro;
      maxAltIdx = i;
    }
    if (p.gs != null && p.gs > maxGs) {
      maxGs = p.gs;
      maxGsIdx = i;
    }
    if (p.lat != null && p.lon != null && recLat != null && recLon != null) {
      const d = haversineNm(recLat, recLon, p.lat, p.lon);
      if (d > maxDist) {
        maxDist = d;
        maxDistLat = p.lat;
        maxDistLon = p.lon;
      }
    }
  }
  return {
    altRate: maxAltIdx >= 0 ? positions[maxAltIdx].baro_rate : null,
    speedTrack: maxGsIdx >= 0 ? positions[maxGsIdx].track : null,
    distBearing:
      maxDistLat != null && maxDistLon != null && recLat != null && recLon != null
        ? bearingFromReceiver(recLat, recLon, maxDistLat, maxDistLon)
        : null,
  };
}

export function FlightHeader({
  detail,
  photoQ,
  positions,
  receiverLat,
  receiverLon,
}: {
  detail: FlightDetail;
  photoQ: { data: PhotoResp | null | undefined; isLoading: boolean };
  positions: Position[];
  receiverLat: number | null;
  receiverLon: number | null;
}) {
  const { fmtAlt, fmtSpd, fmtDist, fmtTs } = useFormat();
  const f = detail.flight;
  // ACARS badge: same deduped query the AcarsPanel below uses, so the badge and
  // the block always agree. Shows only when VDL2 is available and this flight
  // actually has ACARS messages.
  const acars = useVdl2FlightMessages(f.icao_hex, f.first_seen, f.last_seen);
  const hasAcars = acars.available && acars.messages.length > 0;
  const photoUrl =
    safeUrl(photoQ.data?.large_url ?? null) || safeUrl(photoQ.data?.thumbnail_url ?? null);
  const atMax = computeAtMax(positions, receiverLat, receiverLon);

  // Subtitle joins aircraft_type · type_desc · operator · route. Each
  // segment omitted if null so the line never reads ' ·  · '.
  const subtitleParts = [
    f.aircraft_type,
    f.type_desc,
    f.airline_name,
    f.origin_icao || f.dest_icao ? `${f.origin_icao ?? '???'} → ${f.dest_icao ?? '???'}` : null,
  ].filter(Boolean);

  const squawkVariant: 'danger' | 'warn' =
    f.squawk === '7700' || f.squawk === '7600' || f.squawk === '7500' ? 'danger' : 'warn';

  return (
    <Card data-testid="flight-header-card">
      <CardContent className="pt-4">
        {/*
          Laptop / iPad / iPad-portrait: photo left (fixed 140 px), content right.
          iPhone (<sm): photo first (full width 16:9), content below.
        */}
        <div className="flex flex-col gap-3 sm:grid sm:grid-cols-[140px_1fr] sm:gap-3">
          {/* Photo box */}
          <div className="space-y-1">
            {photoQ.isLoading ? (
              <Skeleton className="aspect-[16/9] w-full sm:h-[100px] sm:w-[140px]" />
            ) : photoUrl ? (
              <>
                <PhotoLightbox photo={photoQ.data ?? null} alt={f.registration ?? f.icao_hex}>
                  <button
                    type="button"
                    aria-label="Enlarge photo"
                    data-testid="flight-photo-trigger"
                    className={cn(
                      'block overflow-hidden rounded bg-[var(--color-surface-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
                      'aspect-[16/9] w-full sm:aspect-auto sm:h-[100px] sm:w-[140px]',
                    )}
                  >
                    <img
                      src={photoUrl}
                      alt={f.registration ?? f.icao_hex}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  </button>
                </PhotoLightbox>
                {photoQ.data?.photographer && (
                  <p
                    className="text-[10px] text-[var(--color-text-dim)]"
                    data-testid="flight-photo-credit"
                  >
                    © {photoQ.data.photographer}
                    {photoQ.data.is_type_photo ? ' · type photo' : ''}
                  </p>
                )}
              </>
            ) : (
              <div
                className={cn(
                  'flex items-center justify-center rounded bg-[var(--color-surface-2)] text-xs text-[var(--color-text-dim)]',
                  'aspect-[16/9] sm:aspect-auto sm:h-[100px] sm:w-[140px]',
                )}
              >
                no photo
              </div>
            )}
          </div>

          {/* Identity + subtitle + metric grid */}
          <div className="space-y-2">
            {/* Identity row */}
            <div className="flex flex-wrap items-center gap-2" data-testid="flight-identity">
              <span className="text-base font-semibold">
                <Link
                  to={`/aircraft/${f.icao_hex}`}
                  className="font-mono text-[var(--color-accent)] hover:underline"
                  aria-label={`View aircraft ${f.icao_hex} history`}
                >
                  {f.registration ?? f.icao_hex}
                </Link>
                <span className="mx-1 text-[var(--color-text-dim)]">·</span>
                <span className="font-mono">{f.callsign ?? '—'}</span>
              </span>
              <span className="ml-auto flex flex-wrap items-center gap-1.5">
                <Link
                  to={`/aircraft/${f.icao_hex}`}
                  className="font-mono text-xs text-[var(--color-text-dim)] hover:text-[var(--color-text)]"
                >
                  {f.icao_hex}
                </Link>
                {f.squawk ? <Badge variant={squawkVariant}>{f.squawk}</Badge> : null}
                <FlagBadge flags={f.flags} />
                <SourceBadge source={f.primary_source} size="sm" />
                {hasAcars ? (
                  <Badge
                    variant="default"
                    className="px-1.5 py-0 text-[10px]"
                    data-testid="flight-acars-badge"
                  >
                    ACARS
                  </Badge>
                ) : null}
              </span>
            </div>

            {/* Subtitle line */}
            {subtitleParts.length > 0 ? (
              <div className="text-xs text-[var(--color-text-dim)]" data-testid="flight-subtitle">
                {subtitleParts.join(' · ')}
              </div>
            ) : null}

            {/* 4×2 metric grid (2×4 on iPhone) */}
            <div
              className="grid grid-cols-2 gap-x-4 gap-y-2 pt-1 sm:grid-cols-4"
              data-testid="flight-metric-grid"
            >
              <MetricCell
                label="Max alt"
                value={fmtAlt(f.max_alt_baro)}
                sublabel={
                  atMax.altRate != null
                    ? `vert ${atMax.altRate > 0 ? '+' : ''}${Math.round(atMax.altRate)} ft/min`
                    : undefined
                }
                testid="flight-metric-alt"
              />
              <MetricCell
                label="Max speed"
                value={fmtSpd(f.max_gs)}
                sublabel={
                  atMax.speedTrack != null ? `track ${Math.round(atMax.speedTrack)}°` : undefined
                }
                testid="flight-metric-speed"
              />
              <MetricCell
                label="Max distance"
                value={fmtDist(f.max_distance_nm)}
                sublabel={
                  atMax.distBearing != null
                    ? `bearing ${Math.round(atMax.distBearing)}°`
                    : undefined
                }
                testid="flight-metric-dist"
              />
              <MetricCell
                label="Window"
                value={
                  <span>
                    {fmtTs(f.first_seen)}
                    <span className="mx-1 text-[var(--color-text-dim)]">↘</span>
                    {fmtTs(f.last_seen)}
                  </span>
                }
                valueText={`${fmtTs(f.first_seen)} to ${fmtTs(f.last_seen)}`}
                sublabel={`duration ${fmtDur(f.duration_sec)}`}
                testid="flight-metric-window"
              />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
