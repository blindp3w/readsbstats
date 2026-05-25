import { useQuery } from '@tanstack/react-query';
import { toast } from 'sonner';
import { CopyIcon } from '@radix-ui/react-icons';
import { apiJson } from '@/lib/api';
import { copyToClipboard } from '@/lib/clipboard';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Alert } from '@/components/ui/Alert';
import { Badge } from '@/components/ui/Badge';

// Mirrors the v1 Jinja /settings page. Data comes from GET /api/settings —
// the single source of truth (see web.py::_settings_payload). All sensitive
// values are masked server-side before we ever see them.
//
// Read-only display. No mutations, no "edit settings" UI — env vars are
// changed by editing the systemd service file, which is intentional.

interface SettingMeta {
  env_var: string;
  default: string | number | boolean | null;
  customized: boolean;
}

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
  // Health (subset shown — full set has 16 thresholds, see web.py)
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
  map_history_hours?: number;
  // Telegram
  telegram_token: string; // "configured" | "not set"
  telegram_chat_id: string; // "configured" | "not set"
  telegram_summary_time: string;
  telegram_units: string;
  base_url: string;
  // Backend ships env-var name, parsed default, and customized flag per
  // payload key so frontend never maintains its own table of env-var
  // names (drift defence). May be absent during the server-cache
  // transition window immediately after deploy — handle gracefully.
  _metadata?: Record<string, SettingMeta>;
}

// Each row is (label, displayed value, payload key for metadata lookup).
// The payload key matches a top-level field on `SettingsPayload` so we can
// resolve env-var + default + customized from `_metadata`.
type Row = [label: string, value: string | number | boolean, metaKey: string];

type Section = { title: string; rows: Row[]; testid: string };

function buildSections(s: SettingsPayload): Section[] {
  const fmt = (v: number | string | boolean): string => String(v);
  const toggle = (v: boolean | number): string => (v ? 'enabled' : 'disabled');

  return [
    {
      title: 'Receiver',
      testid: 'settings-section-receiver',
      rows: [
        ['Latitude', fmt(s.lat), 'lat'],
        ['Longitude', fmt(s.lon), 'lon'],
        ['Max range (NM)', fmt(s.max_range), 'max_range'],
      ],
    },
    {
      title: 'Collector',
      testid: 'settings-section-collector',
      rows: [
        ['Poll interval (s)', fmt(s.poll_interval), 'poll_interval'],
        ['Flight gap (s)', fmt(s.flight_gap), 'flight_gap'],
        ['Min positions kept', fmt(s.min_positions), 'min_positions'],
        ['Max seen position (s)', fmt(s.max_seen_pos), 'max_seen_pos'],
        ['Max speed (kts)', fmt(s.max_speed_kts), 'max_speed_kts'],
      ],
    },
    {
      title: 'Database',
      testid: 'settings-section-database',
      rows: [
        ['Database file', s.db_path, 'db_path'],
        ['Retention (days)', fmt(s.retention_days), 'retention_days'],
        ['Purge interval (s)', fmt(s.purge_interval), 'purge_interval'],
      ],
    },
    {
      title: 'Enrichment',
      testid: 'settings-section-enrichment',
      rows: [
        ['Photo cache (days)', fmt(s.photo_cache_days), 'photo_cache_days'],
        ['Airspace GeoJSON', s.airspace_geojson, 'airspace_geojson'],
        ['Route cache (days)', fmt(s.route_cache_days), 'route_cache_days'],
        ['Route enrich interval (s)', fmt(s.route_interval), 'route_interval'],
        ['Route batch size', fmt(s.route_batch), 'route_batch'],
        ['Route rate limit (s)', fmt(s.route_rate_limit), 'route_rate_limit'],
      ],
    },
    {
      title: 'External ADS-B (airplanes.live)',
      testid: 'settings-section-adsbx',
      rows: [
        ['Enabled', toggle(s.adsbx_enabled), 'adsbx_enabled'],
        ['Poll interval (s)', fmt(s.adsbx_interval), 'adsbx_interval'],
        ['Range (NM)', fmt(s.adsbx_range), 'adsbx_range'],
        ['API URL', s.adsbx_url, 'adsbx_url'],
      ],
    },
    {
      title: 'Receiver metrics',
      testid: 'settings-section-metrics',
      rows: [
        ['Enabled', toggle(s.metrics_enabled), 'metrics_enabled'],
        ['Poll interval (s)', fmt(s.metrics_interval), 'metrics_interval'],
        ['stats.json path', s.stats_json, 'stats_json'],
      ],
    },
    {
      title: 'Health checks',
      testid: 'settings-section-health',
      rows: [
        ['Heartbeat warn (s)', fmt(s.health_heartbeat_warn_s), 'health_heartbeat_warn_s'],
        ['Heartbeat critical (s)', fmt(s.health_heartbeat_crit_s), 'health_heartbeat_crit_s'],
        ['Aircraft gap (s)', fmt(s.health_aircraft_gap_s), 'health_aircraft_gap_s'],
        ['Noise warn (dB)', fmt(s.health_noise_warn_db), 'health_noise_warn_db'],
        ['Noise critical (dB)', fmt(s.health_noise_crit_db), 'health_noise_crit_db'],
        ['CPU warn (%)', fmt(s.health_cpu_warn_pct), 'health_cpu_warn_pct'],
        ['CPU critical (%)', fmt(s.health_cpu_crit_pct), 'health_cpu_crit_pct'],
        ['Baseline window (weeks)', fmt(s.health_baseline_weeks), 'health_baseline_weeks'],
        ['Baseline min samples', fmt(s.health_baseline_min_samples), 'health_baseline_min_samples'],
        ['Message drop (%)', fmt(s.health_msg_drop_pct), 'health_msg_drop_pct'],
        ['Aircraft drop (%)', fmt(s.health_aircraft_drop_pct), 'health_aircraft_drop_pct'],
        ['Signal drop (dB)', fmt(s.health_signal_drop_db), 'health_signal_drop_db'],
        ['Gain strong (%)', fmt(s.health_gain_strong_pct), 'health_gain_strong_pct'],
        ['Range short (days)', fmt(s.health_range_short_days), 'health_range_short_days'],
        ['Range long (days)', fmt(s.health_range_long_days), 'health_range_long_days'],
        ['Range ratio', fmt(s.health_range_ratio), 'health_range_ratio'],
      ],
    },
    {
      title: 'Web server',
      testid: 'settings-section-web',
      rows: [['Root path (nginx prefix)', s.root_path, 'root_path']],
    },
    {
      title: 'UI defaults',
      testid: 'settings-section-ui',
      rows: [
        ['Default page size', fmt(s.page_size), 'page_size'],
        ['Max page size', fmt(s.max_page_size), 'max_page_size'],
        ['Clock format', s.time_format, 'time_format'],
      ],
    },
    {
      title: 'Telegram notifications',
      testid: 'settings-section-telegram',
      rows: [
        ['Bot token', s.telegram_token, 'telegram_token'],
        ['Chat ID', s.telegram_chat_id, 'telegram_chat_id'],
        ['Summary time (local)', s.telegram_summary_time, 'telegram_summary_time'],
        ['Units', s.telegram_units, 'telegram_units'],
        ['Base URL', s.base_url, 'base_url'],
      ],
    },
  ];
}

// Display strings that already convey "this is the default state" by their
// own wording. Suppress the "(default)" suffix on these so the value
// doesn't read as e.g. "not set (default)".
const IMPLIES_DEFAULT = new Set(['not set', '(not set)', '(bundled poland.geojson)', 'disabled']);

function ValueCell({ value, meta }: { value: string | number | boolean; meta?: SettingMeta }) {
  const text = typeof value === 'string' ? value : String(value);
  const showBadge = text === 'configured' || text === 'enabled';
  const showMutedBadge = text === 'not set' || text === 'disabled';
  const showDefault = meta != null && meta.customized === false && !IMPLIES_DEFAULT.has(text);
  return (
    <span className="text-sm tabnum">
      {showBadge ? (
        <Badge variant="success">{text}</Badge>
      ) : showMutedBadge ? (
        <Badge variant="muted">{text}</Badge>
      ) : (
        text
      )}
      {showDefault && <span className="ml-2 text-xs text-[var(--color-text-dim)]">(default)</span>}
    </span>
  );
}

function CopyEnvVarButton({ envVar }: { envVar?: string }) {
  if (!envVar) return null;
  const handleCopy = async () => {
    const ok = await copyToClipboard(envVar);
    if (ok) {
      toast(`Copied ${envVar}`);
    } else {
      toast(`Couldn't copy — long-press to select`);
    }
  };
  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={`Copy ${envVar}`}
      className="group inline-flex min-h-[36px] items-center gap-1.5 rounded
                 px-1.5 py-1 font-mono text-xs text-[var(--color-text-dim)]
                 transition-colors
                 hover:bg-[var(--color-surface-3)] hover:text-[var(--color-text)]
                 focus-visible:outline-none focus-visible:ring-2
                 focus-visible:ring-[var(--color-accent)]
                 active:bg-[var(--color-surface-3)]"
    >
      <code className="font-mono">{envVar}</code>
      <CopyIcon
        aria-hidden="true"
        className="h-3.5 w-3.5 opacity-0 transition-opacity group-hover:opacity-60 group-focus-visible:opacity-60"
      />
    </button>
  );
}

function SettingRow({
  label,
  value,
  meta,
}: {
  label: string;
  value: string | number | boolean;
  meta?: SettingMeta;
}) {
  return (
    <div
      className="grid grid-cols-1 gap-y-1 border-t border-[var(--color-border-default)]
                 py-2 first:border-t-0
                 md:grid-cols-[1fr_auto_minmax(0,1fr)] md:items-center
                 md:gap-x-4 md:py-1.5"
      data-testid="settings-row"
    >
      <div className="text-sm font-medium text-[var(--color-text)]">{label}</div>
      <div>
        <ValueCell value={value} meta={meta} />
      </div>
      <div className="md:justify-self-end">
        <CopyEnvVarButton envVar={meta?.env_var} />
      </div>
    </div>
  );
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
              <div role="list">
                {section.rows.map(([label, value, metaKey]) => (
                  <SettingRow
                    key={label}
                    label={label}
                    value={value}
                    meta={q.data._metadata?.[metaKey]}
                  />
                ))}
              </div>
            </CardContent>
          </Card>
        ))}

      <Card data-testid="settings-build">
        <CardHeader>
          <CardTitle>Build info</CardTitle>
        </CardHeader>
        <CardContent className="space-y-1">
          <p className="text-sm text-[var(--color-text-dim)]">
            App version:{' '}
            <code
              data-testid="settings-app-version"
              className="tabnum rounded bg-[var(--color-surface-2)] px-1 py-0.5 text-xs text-[var(--color-text)]"
            >
              {__APP_VERSION__}
            </code>
          </p>
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
