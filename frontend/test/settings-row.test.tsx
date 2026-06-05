import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { TooltipProvider } from '@/components/ui/Tooltip';
import SettingsPage from '@/pages/Settings';

// Sonner toast is invoked when the copy button fires. Replace it with a
// spy so we can assert without needing the toaster mounted.
const toastSpy = vi.fn();
vi.mock('sonner', () => ({
  toast: (...args: unknown[]) => toastSpy(...args),
  Toaster: () => null,
}));

// Minimal /api/settings payload exercising the three behaviours we care
// about: customized=false (default suffix), customized=true (no suffix),
// secret-masked (no leak), and a key whose display value already implies
// default (suffix suppressed).
const SAMPLE_PAYLOAD = {
  lat: 52.0,
  lon: 21.0,
  max_range: 450,
  poll_interval: 5,
  flight_gap: 1800,
  min_positions: 2,
  max_seen_pos: 60,
  max_speed_kts: 2000,
  db_path: 'history.db',
  retention_days: 0,
  purge_interval: 3600,
  photo_cache_days: 30,
  airspace_geojson: '(bundled poland.geojson)',
  route_cache_days: 30,
  route_interval: 60,
  route_batch: 20,
  route_rate_limit: 1.0,
  adsbx_enabled: 1,
  adsbx_interval: 60,
  adsbx_range: 250,
  adsbx_url: 'https://api.airplanes.live/v2',
  metrics_enabled: 0,
  metrics_interval: 60,
  stats_json: '(not set)',
  health_heartbeat_warn_s: 120,
  health_heartbeat_crit_s: 300,
  health_aircraft_gap_s: 600,
  health_noise_warn_db: -28,
  health_noise_crit_db: -25,
  health_cpu_warn_pct: 80,
  health_cpu_crit_pct: 90,
  health_baseline_weeks: 4,
  health_baseline_min_samples: 3,
  health_msg_drop_pct: 50,
  health_aircraft_drop_pct: 25,
  health_signal_drop_db: 3,
  health_gain_strong_pct: 5,
  health_range_short_days: 7,
  health_range_long_days: 30,
  health_range_ratio: 0.85,
  root_path: '/stats',
  page_size: 100,
  max_page_size: 500,
  time_format: '24h',
  telegram_token: 'not set',
  telegram_chat_id: 'not set',
  telegram_summary_time: '21:00',
  telegram_units: 'metric',
  base_url: 'http://homepi.local/stats',
  vdl2_enabled: 1,
  vdl2_db_path: 'vdl2.db',
  vdl2_retention: 90,
  _metadata: {
    // Customized: max_range was set to 450 (default), so customized=false
    // → "(default)" suffix should appear next to the value.
    max_range: { env_var: 'RSBS_MAX_RANGE', default: 450, customized: false },
    // Customized: poll_interval differs from default 5 — but our payload
    // also has 5, so for this fixture customized=false. To exercise the
    // "no suffix" branch we mark flight_gap as customized=true below.
    poll_interval: { env_var: 'RSBS_POLL_INTERVAL', default: 5, customized: false },
    flight_gap: { env_var: 'RSBS_FLIGHT_GAP', default: 1800, customized: true },
    // Secret-masked: default is null (server stripped the path).
    db_path: { env_var: 'RSBS_DB_PATH', default: null, customized: false },
    // Display value "not set" implies default; suffix should be suppressed.
    telegram_token: { env_var: 'RSBS_TELEGRAM_TOKEN', default: '', customized: false },
    vdl2_enabled: { env_var: 'RSBS_VDL2_ENABLED', default: false, customized: true },
    vdl2_retention: { env_var: 'RSBS_VDL2_RETENTION_DAYS', default: 90, customized: false },
  },
};

function setupFetch(body: object = SAMPLE_PAYLOAD): void {
  globalThis.fetch = vi.fn(async () => {
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function renderSettings() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <SettingsPage />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('Settings page — _metadata-driven rendering', () => {
  beforeEach(() => {
    toastSpy.mockReset();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders env-var names from /api/settings._metadata, not hardcoded', async () => {
    setupFetch();
    renderSettings();
    // Each registered env var should appear as a copy button on its row.
    await waitFor(() => {
      expect(screen.getByLabelText('Copy RSBS_MAX_RANGE')).toBeInTheDocument();
    });
    expect(screen.getByLabelText('Copy RSBS_POLL_INTERVAL')).toBeInTheDocument();
    expect(screen.getByLabelText('Copy RSBS_FLIGHT_GAP')).toBeInTheDocument();
    expect(screen.getByLabelText('Copy RSBS_DB_PATH')).toBeInTheDocument();
  });

  it('appends "(default)" suffix on rows whose meta.customized is false', async () => {
    setupFetch();
    renderSettings();
    // max_range value cell should contain "450" + "(default)".
    await screen.findByLabelText('Copy RSBS_MAX_RANGE');
    const maxRangeButton = screen.getByLabelText('Copy RSBS_MAX_RANGE');
    const row = maxRangeButton.closest('[data-testid="settings-row"]');
    expect(row).not.toBeNull();
    expect(row!.textContent).toContain('450');
    expect(row!.textContent).toContain('(default)');
  });

  it('does NOT append "(default)" on customized rows', async () => {
    setupFetch();
    renderSettings();
    await screen.findByLabelText('Copy RSBS_FLIGHT_GAP');
    const button = screen.getByLabelText('Copy RSBS_FLIGHT_GAP');
    const row = button.closest('[data-testid="settings-row"]');
    expect(row).not.toBeNull();
    expect(row!.textContent).toContain('1800');
    expect(row!.textContent).not.toContain('(default)');
  });

  it('suppresses "(default)" when display value already implies default', async () => {
    setupFetch();
    renderSettings();
    // telegram_token displays "not set"; suffix would read as "not set (default)" — suppressed.
    await screen.findByLabelText('Copy RSBS_TELEGRAM_TOKEN');
    const row = screen
      .getByLabelText('Copy RSBS_TELEGRAM_TOKEN')
      .closest('[data-testid="settings-row"]');
    expect(row).not.toBeNull();
    expect(row!.textContent).toContain('not set');
    expect(row!.textContent).not.toContain('(default)');
  });

  it('clicking the env-var button calls copyToClipboard and fires a toast', async () => {
    setupFetch();
    // execCommand is the fallback path; jsdom is a non-secure context.
    document.execCommand = vi.fn(() => true);
    renderSettings();
    const btn = await screen.findByLabelText('Copy RSBS_MAX_RANGE');
    fireEvent.click(btn);
    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith('Copied RSBS_MAX_RANGE');
    });
    expect(document.execCommand).toHaveBeenCalledWith('copy');
  });

  it('renders the VDL2 / ACARS section with enabled, db file, and retention rows', async () => {
    setupFetch();
    renderSettings();
    const section = await waitFor(() => screen.getByTestId('settings-section-vdl2'));
    expect(section.textContent).toContain('VDL2 / ACARS');
    expect(section.textContent).toContain('vdl2.db');
    expect(section.textContent).toContain('90');
    // enabled flag renders the success badge ("enabled"), and its env var is copyable.
    expect(screen.getByLabelText('Copy RSBS_VDL2_ENABLED')).toBeInTheDocument();
    expect(section.textContent).toContain('enabled');
  });

  it('renders without crashing when _metadata is missing from response', async () => {
    // Simulates the server-cache transition window immediately after deploy:
    // the payload is the legacy shape without _metadata. No copy buttons,
    // no (default) suffix, but the page must still mount cleanly.
    const legacy = { ...SAMPLE_PAYLOAD } as Partial<typeof SAMPLE_PAYLOAD>;
    delete legacy._metadata;
    setupFetch(legacy);
    renderSettings();
    // Wait for the query to resolve and the section card to render.
    await screen.findByText('Receiver');
    // Header is rendered.
    expect(screen.getByText('Settings')).toBeInTheDocument();
    // No copy buttons because every meta is undefined.
    expect(screen.queryByLabelText('Copy RSBS_MAX_RANGE')).not.toBeInTheDocument();
    // No (default) suffix anywhere — every row's meta is undefined so
    // the suffix branch is unreachable.
    const allRows = screen.getAllByTestId('settings-row');
    for (const row of allRows) {
      expect(row.textContent).not.toContain('(default)');
    }
  });
});
