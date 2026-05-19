import { useQuery } from '@tanstack/react-query';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';

// Mirrors the v1 Jinja /settings page. Data comes from GET /api/settings —
// the single source of truth (see web.py::_settings_payload). All sensitive
// values are masked server-side before we ever see them.
//
// Read-only display. No mutations, no "edit settings" UI — env vars are
// changed by editing the systemd service file, which is intentional.

interface SettingsPayload {
  // Receiver
  lat: number;
  lon: number;
  max_range: number;
  // Collector
  poll_interval: number;
  flight_gap: number;
  min_positions: number;
  max_seen_pos: number;
  max_speed_kts: number;
  // Database
  db_path: string;
  retention_days: number;
  purge_interval: number;
  // Enrichment
  photo_cache_days: number;
  airspace_geojson: string;
  route_cache_days: number;
  route_interval: number;
  route_batch: number;
  route_rate_limit: number;
  // External ADS-B
  adsbx_enabled: boolean | number;
  adsbx_interval: number;
  adsbx_range: number;
  adsbx_url: string;
  // Metrics
  metrics_enabled: boolean | number;
  metrics_interval: number;
  stats_json: string;
  // Health (subset shown — full set has 14 thresholds, see web.py)
  health_heartbeat_warn_s: number;
  health_heartbeat_crit_s: number;
  health_aircraft_gap_s: number;
  health_noise_warn_db: number;
  health_noise_crit_db: number;
  health_cpu_warn_pct: number;
  health_cpu_crit_pct: number;
  health_baseline_weeks: number;
  health_baseline_min_samples: number;
  health_msg_drop_pct: number;
  health_aircraft_drop_pct: number;
  health_signal_drop_db: number;
  health_gain_strong_pct: number;
  health_range_short_days: number;
  health_range_long_days: number;
  health_range_ratio: number;
  // Web (web_host / web_port intentionally omitted by the backend — see
  // audit-12 #171; the client is already at that URL).
  root_path: string;
  // UI
  page_size: number;
  max_page_size: number;
  time_format: string;
  // Telegram
  telegram_token: string; // "configured" | "not set"
  telegram_chat_id: string; // "configured" | "not set"
  telegram_summary_time: string;
  telegram_units: string;
  base_url: string;
}

type Row = [label: string, value: string | number | boolean, hint?: string];

type Section = { title: string; rows: Row[]; testid: string };

function buildSections(s: SettingsPayload): Section[] {
  const fmt = (v: number | string | boolean): string => {
    if (typeof v === 'boolean') return v ? 'enabled' : 'disabled';
    if (typeof v === 'number' && (v === 0 || v === 1) && Number.isInteger(v)) {
      // toggle-style fields — keep as number, caller decides
    }
    return String(v);
  };
  const toggle = (v: boolean | number): string => (v ? 'enabled' : 'disabled');

  return [
    {
      title: 'Receiver',
      testid: 'settings-section-receiver',
      rows: [
        ['Latitude', fmt(s.lat), 'RSBS_LAT'],
        ['Longitude', fmt(s.lon), 'RSBS_LON'],
        ['Max range (NM)', fmt(s.max_range), 'RSBS_MAX_RANGE_NM'],
      ],
    },
    {
      title: 'Collector',
      testid: 'settings-section-collector',
      rows: [
        ['Poll interval (s)', fmt(s.poll_interval), 'RSBS_POLL_INTERVAL'],
        ['Flight gap (s)', fmt(s.flight_gap), 'RSBS_FLIGHT_GAP'],
        ['Min positions kept', fmt(s.min_positions), 'RSBS_MIN_POSITIONS_KEEP'],
        ['Max seen position (s)', fmt(s.max_seen_pos), 'RSBS_MAX_SEEN_POS'],
        ['Max speed (kts)', fmt(s.max_speed_kts), 'RSBS_MAX_SPEED_KTS'],
      ],
    },
    {
      title: 'Database',
      testid: 'settings-section-database',
      rows: [
        ['Database file', s.db_path, 'RSBS_DB_PATH (basename only)'],
        ['Retention (days)', fmt(s.retention_days), 'RSBS_RETENTION_DAYS'],
        ['Purge interval (s)', fmt(s.purge_interval), 'RSBS_PURGE_INTERVAL'],
      ],
    },
    {
      title: 'Enrichment',
      testid: 'settings-section-enrichment',
      rows: [
        ['Photo cache (days)', fmt(s.photo_cache_days), 'RSBS_PHOTO_CACHE_DAYS'],
        ['Airspace GeoJSON', s.airspace_geojson, 'RSBS_AIRSPACE_GEOJSON'],
        ['Route cache (days)', fmt(s.route_cache_days), 'RSBS_ROUTE_CACHE_DAYS'],
        ['Route enrich interval (s)', fmt(s.route_interval), 'RSBS_ROUTE_ENRICH_INTERVAL'],
        ['Route batch size', fmt(s.route_batch), 'RSBS_ROUTE_BATCH_SIZE'],
        ['Route rate limit (s)', fmt(s.route_rate_limit), 'RSBS_ROUTE_RATE_LIMIT'],
      ],
    },
    {
      title: 'External ADS-B (airplanes.live)',
      testid: 'settings-section-adsbx',
      rows: [
        ['Enabled', toggle(s.adsbx_enabled), 'RSBS_ADSBX_ENABLED'],
        ['Poll interval (s)', fmt(s.adsbx_interval), 'RSBS_ADSBX_POLL_INTERVAL'],
        ['Range (NM)', fmt(s.adsbx_range), 'RSBS_ADSBX_RANGE_NM'],
        ['API URL', s.adsbx_url, 'RSBS_ADSBX_API_URL'],
      ],
    },
    {
      title: 'Receiver metrics',
      testid: 'settings-section-metrics',
      rows: [
        ['Enabled', toggle(s.metrics_enabled), 'RSBS_METRICS_ENABLED'],
        ['Poll interval (s)', fmt(s.metrics_interval), 'RSBS_METRICS_INTERVAL'],
        ['stats.json path', s.stats_json, 'RSBS_STATS_JSON'],
      ],
    },
    {
      title: 'Health checks',
      testid: 'settings-section-health',
      rows: [
        ['Heartbeat warn (s)', fmt(s.health_heartbeat_warn_s), ''],
        ['Heartbeat critical (s)', fmt(s.health_heartbeat_crit_s), ''],
        ['Aircraft gap (s)', fmt(s.health_aircraft_gap_s), ''],
        ['Noise warn (dB)', fmt(s.health_noise_warn_db), ''],
        ['Noise critical (dB)', fmt(s.health_noise_crit_db), ''],
        ['CPU warn (%)', fmt(s.health_cpu_warn_pct), ''],
        ['CPU critical (%)', fmt(s.health_cpu_crit_pct), ''],
        ['Baseline window (weeks)', fmt(s.health_baseline_weeks), ''],
        ['Baseline min samples', fmt(s.health_baseline_min_samples), ''],
        ['Message drop (%)', fmt(s.health_msg_drop_pct), ''],
        ['Aircraft drop (%)', fmt(s.health_aircraft_drop_pct), ''],
        ['Signal drop (dB)', fmt(s.health_signal_drop_db), ''],
        ['Gain strong (%)', fmt(s.health_gain_strong_pct), ''],
        ['Range short (days)', fmt(s.health_range_short_days), ''],
        ['Range long (days)', fmt(s.health_range_long_days), ''],
        ['Range ratio', fmt(s.health_range_ratio), ''],
      ],
    },
    {
      title: 'Web server',
      testid: 'settings-section-web',
      rows: [
        ['Root path (nginx prefix)', s.root_path, 'RSBS_ROOT_PATH'],
      ],
    },
    {
      title: 'UI defaults',
      testid: 'settings-section-ui',
      rows: [
        ['Default page size', fmt(s.page_size), 'RSBS_PAGE_SIZE'],
        ['Max page size', fmt(s.max_page_size), 'RSBS_MAX_PAGE_SIZE'],
        ['Clock format', s.time_format, 'RSBS_TIME_FORMAT'],
      ],
    },
    {
      title: 'Telegram notifications',
      testid: 'settings-section-telegram',
      rows: [
        ['Bot token', s.telegram_token, 'masked'],
        ['Chat ID', s.telegram_chat_id, 'masked'],
        ['Summary time (local)', s.telegram_summary_time, 'RSBS_SUMMARY_TIME'],
        ['Units', s.telegram_units, 'RSBS_TELEGRAM_UNITS'],
        ['Base URL', s.base_url, 'RSBS_TELEGRAM_BASE_URL'],
      ],
    },
  ];
}

export default function SettingsPage() {
  const q = useQuery<SettingsPayload>({
    queryKey: ['settings'],
    queryFn: () => apiJson<SettingsPayload>('settings'),
    // Settings reflect deploy-time env vars; refresh rarely.
    staleTime: 60_000,
  });

  return (
    <div className="mx-auto max-w-5xl space-y-4 px-4 py-6" data-testid="page-settings">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-[var(--color-text-dim)]">
          Runtime configuration. Read-only — change via{' '}
          <code className="rounded bg-[var(--color-surface-2)] px-1 py-0.5 text-xs">
            systemctl edit readsbstats-web
          </code>
          .
        </p>
      </header>

      {q.isError && (
        <Alert variant="error" data-testid="settings-error">
          Failed to load settings: {(q.error as Error).message}
        </Alert>
      )}

      {q.isLoading &&
        Array.from({ length: 3 }).map((_, i) => (
          <Card key={i} data-testid="settings-skeleton">
            <CardHeader>
              <Skeleton className="h-5 w-32" />
            </CardHeader>
            <CardContent>
              <Skeleton className="h-32 w-full" />
            </CardContent>
          </Card>
        ))}

      {q.data &&
        buildSections(q.data).map((section) => (
          <Card key={section.title} data-testid={section.testid}>
            <CardHeader>
              <CardTitle>{section.title}</CardTitle>
            </CardHeader>
            <CardContent>
              <Table>
                <THead>
                  <TR>
                    <TH className="w-1/2 md:w-1/3">Setting</TH>
                    <TH>Value</TH>
                    <TH className="hidden md:table-cell">Env var</TH>
                  </TR>
                </THead>
                <TBody>
                  {section.rows.map(([label, value, hint]) => (
                    <TR key={label}>
                      <TH scope="row" className="font-normal text-[var(--color-text)]">
                        {label}
                      </TH>
                      <TD className="tabnum">
                        {typeof value === 'string' && (value === 'configured' || value === 'enabled') ? (
                          <Badge variant="success">{value}</Badge>
                        ) : typeof value === 'string' && (value === 'not set' || value === 'disabled') ? (
                          <Badge variant="muted">{value}</Badge>
                        ) : (
                          String(value)
                        )}
                      </TD>
                      <TD className="hidden md:table-cell text-xs text-[var(--color-text-dim)]">
                        {hint ? <code>{hint}</code> : ''}
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            </CardContent>
          </Card>
        ))}

      <Card data-testid="settings-build">
        <CardHeader>
          <CardTitle>Build info</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-[var(--color-text-dim)]">
            Frontend build:{' '}
            <code className="tabnum rounded bg-[var(--color-surface-2)] px-1 py-0.5 text-xs text-[var(--color-text)]">
              {__FRONTEND_BUILD__}
            </code>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
