# VDL2 reception two-chart redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the VDL2 reception KPI card on the Metrics page with two ECharts — a message-rate area line and a per-frequency small-multiples panel — driven by the page's existing time-range picker.

**Architecture:** A new `GET /api/vdl2/timeseries?from&to` aggregates `vdl2_messages` into time buckets and returns the SAME columnar shape as `/api/metrics` (`{bucket_seconds, metrics, data}`), so the frontend reuses the existing `buildPanelOption` and `buildSignalSmallMultiplesOption` chart builders unchanged. `Vdl2ReceptionCard` is rebuilt to render the two charts from that response and share `Metrics.tsx`'s `from`/`to` range state. The old `/api/vdl2/reception` endpoint, schemas, and card internals are removed.

**Tech stack:** FastAPI + SQLite (backend), React 19 + TanStack Query + ECharts (frontend). Spec: `docs/superpowers/specs/2026-06-05-vdl2-reception-2-charts-design.md`.

**Branch:** `feat/vdl2-integrations` (already checked out; PR #11).

---

## File structure

- **Modify** `src/readsbstats/schemas.py` — add `Vdl2TimeseriesResponse`; later remove `Vdl2ReceptionResponse`/`Vdl2FreqStat`.
- **Modify** `src/readsbstats/api/vdl2.py` — add `_timeseries_bucket`, `_fmt_freq`, `_compute_timeseries`, `api_vdl2_timeseries`; later remove `api_vdl2_reception`/`_compute_reception` (keep `_rate_buckets` — still used by stats).
- **Modify** `tests/test_api_vdl2.py` — add `TestTimeseries`; later remove `TestReception` + the reception 503 line.
- **Modify** `frontend/src/pages/metricsCharts.ts` — add exported `smallMultHeight(n)` helper.
- **Modify** `frontend/src/lib/types.ts` — add `Vdl2TimeseriesResp`; later remove `Vdl2ReceptionResponse`/`Vdl2FreqStat`.
- **Rewrite** `frontend/src/components/metrics/Vdl2ReceptionCard.tsx` — two charts + slim header.
- **Modify** `frontend/src/pages/Metrics.tsx` — pass `from`/`to` to the card.
- **Rewrite** `frontend/test/vdl2-reception.test.tsx` — test the new card.
- **Modify** `CHANGELOG.md`, `README.md`, `docs/development.md`, `docs/api.md`.

Ordering keeps every commit green: add the new endpoint first (reception still present), switch the frontend to it, then remove reception.

---

## Task 1: Backend timeseries response schema

**Files:**
- Modify: `src/readsbstats/schemas.py` (after `Vdl2PositionsResponse`)

- [ ] **Step 1: Add the schema**

In `src/readsbstats/schemas.py`, add after the `Vdl2PositionsResponse` class:

```python
class Vdl2TimeseriesResponse(ApiModel):
    """Bucketed VDL2 reception time-series for the Metrics page, in the same
    columnar shape as /api/metrics so the frontend chart builders are reused.
    Series values are normalized to msgs/min; `total` is the raw count in the
    window (the series must NOT be summed to get a count)."""
    bucket_seconds: int = 0
    metrics: list[str] = []           # ["rate", "<freq>", ...]
    freqs: list[float] = []           # the top frequencies, same order as metrics[1:]
    total: int = 0
    newest_ts: Optional[int] = None
    newest_age_sec: Optional[int] = None
    data: list[list[float]] = []      # [[ts...], [rate...], [freq1...], ...]
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from readsbstats.schemas import Vdl2TimeseriesResponse; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add src/readsbstats/schemas.py
git commit -m "feat(vdl2): add Vdl2TimeseriesResponse schema"
```

---

## Task 2: Backend `/api/vdl2/timeseries` endpoint (TDD)

**Files:**
- Modify: `src/readsbstats/api/vdl2.py`
- Test: `tests/test_api_vdl2.py` (add `TestTimeseries`)

- [ ] **Step 1: Write the failing tests**

In `tests/test_api_vdl2.py`, add this class after `class TestReception:` (anywhere at module level is fine):

```python
class TestTimeseries:
    def _make(self, monkeypatch, rows):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)
        vconn = make_vdl2_db()
        vdl2_db.insert_messages(vconn, rows)
        vconn.commit()
        monkeypatch.setattr(vdl2_db, "_conn", vconn)
        monkeypatch.setattr(_deps, "_db", make_db())
        app = FastAPI()
        web._include_optional_routers(app)
        return vconn, app

    def test_buckets_normalize_and_zero_fill(self, monkeypatch):
        now = int(time.time())
        # Two messages in one minute, none in the next → 2/min then 0/min.
        vconn, app = self._make(monkeypatch, [
            {"ts": now - 90, "icao_hex": "48e95d", "freq": 136.725, "body": "a"},
            {"ts": now - 80, "icao_hex": "48af11", "freq": 136.725, "body": "b"},
        ])
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/timeseries?from={now - 180}&to={now}").json()
        assert data["bucket_seconds"] == 60            # <=24h span → per-minute
        assert data["metrics"][0] == "rate"
        assert data["total"] == 2
        ts_col, rate_col = data["data"][0], data["data"][1]
        assert len(ts_col) == 3                        # 180s / 60s = 3 buckets
        assert max(rate_col) == 2.0                    # 2 msgs in a 60s bucket = 2/min
        assert min(rate_col) == 0.0                    # a quiet bucket is zero-filled

    def test_top_freqs_capped_and_rate_counts_all(self, monkeypatch):
        now = int(time.time())
        rows = []
        # 7 distinct freqs; freq 136.700 has the most messages.
        for i, f in enumerate([136.700, 136.725, 136.775, 136.825, 136.875, 136.925, 136.975]):
            for _ in range(7 - i):
                rows.append({"ts": now - 100, "icao_hex": f"48{i:04x}", "freq": f, "body": "x"})
        vconn, app = self._make(monkeypatch, rows)
        with TestClient(app) as c:
            data = c.get(f"/api/vdl2/timeseries?from={now - 600}&to={now}").json()
        assert len(data["freqs"]) == 6                 # capped at top-6
        assert data["freqs"][0] == 136.7               # most messages first
        assert 136.975 not in data["freqs"]            # the least-active 7th is dropped
        assert data["total"] == sum(range(1, 8))       # rate totals ALL messages, not just top-6

    def test_window_validation_and_503(self, monkeypatch):
        now = int(time.time())
        vconn, app = self._make(monkeypatch, [])
        with TestClient(app) as c:
            assert c.get(f"/api/vdl2/timeseries?from={now}&to={now}").status_code == 400
        vconn.close()

    def test_503_when_db_unavailable(self, monkeypatch):
        monkeypatch.setattr(config, "VDL2_ENABLED", True)

        def boom(*a, **k):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(vdl2_db, "web_conn", boom)
        app = FastAPI()
        web._include_optional_routers(app)
        with TestClient(app) as c:
            assert c.get("/api/vdl2/timeseries").status_code == 503
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_api_vdl2.py -q -k Timeseries`
Expected: FAIL (404 — endpoint not defined yet)

- [ ] **Step 3: Implement the endpoint**

In `src/readsbstats/api/vdl2.py`, add near the other helpers (e.g. just above the `/api/vdl2/active` route). `from fastapi import ... Query` and `cache`, `time`, `log`, `schemas`, `vdl2_db`, `_vdl2_guard`, `HTTPException` are already imported in this module:

```python
_TIMESERIES_TOP_FREQS = 6


def _timeseries_bucket(span: int) -> int:
    """Bucket width (seconds) for a window span. Mirrors /api/metrics, but with a
    60 s minimum — vdl2_messages are individual rows, so there is no raw mode."""
    if span <= 86_400:          # <= 24 h
        return 60
    if span <= 604_800:         # <= 7 d
        return 300
    if span <= 2_592_000:       # <= 30 d
        return 900
    if span <= 7_776_000:       # <= 90 d
        return 3600
    return 14400                # > 90 d


def _fmt_freq(f: float) -> str:
    return f"{f:g}"             # 136.725 -> "136.725", 136.9 -> "136.9"


@router.get("/api/vdl2/timeseries", response_model=schemas.Vdl2TimeseriesResponse,
            response_model_exclude_unset=True)
def api_vdl2_timeseries(
    from_ts: int | None = Query(None, alias="from", ge=0),
    to_ts: int | None = Query(None, alias="to", ge=0),
) -> dict:
    """Bucketed reception time-series (msgs/min total + per top-frequency) for the
    Metrics page's two VDL2 charts, over the picker's [from, to] window. Columnar
    like /api/metrics so the frontend reuses its chart builders. Not cached — from/to
    are stable per page mount and the SPA holds a 30 s staleTime."""
    now = int(time.time())
    if to_ts is None:
        to_ts = now
    if from_ts is None:
        from_ts = to_ts - 86_400
    if to_ts <= from_ts:
        raise HTTPException(400, "to must be greater than from")
    with _vdl2_guard():
        t0 = time.perf_counter()
        result = _compute_timeseries(from_ts, to_ts)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 250:
            log.warning("vdl2 timeseries query slow: %.0f ms", elapsed_ms)
        return result


def _compute_timeseries(from_ts: int, to_ts: int) -> dict:
    conn = vdl2_db.web_conn()
    bucket = _timeseries_bucket(to_ts - from_ts)

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM vdl2_messages WHERE ts >= ? AND ts < ?",
        (from_ts, to_ts),
    ).fetchone()["n"]
    newest = conn.execute("SELECT MAX(ts) AS newest FROM vdl2_messages").fetchone()["newest"]

    top = [
        r["f"] for r in conn.execute(
            "SELECT ROUND(freq, 3) AS f, COUNT(*) AS c FROM vdl2_messages "
            "WHERE freq IS NOT NULL AND ts >= ? AND ts < ? "
            "GROUP BY f ORDER BY c DESC, f LIMIT ?",
            (from_ts, to_ts, _TIMESERIES_TOP_FREQS),
        ).fetchall()
    ]

    # Zero-filled bucket grid (epoch seconds) so quiet bins read 0, not interpolated.
    start = (from_ts // bucket) * bucket
    buckets = list(range(start, to_ts, bucket))
    idx = {b: i for i, b in enumerate(buckets)}
    n = len(buckets)

    rate = [0] * n
    for r in conn.execute(
        "SELECT (ts / ?) * ? AS b, COUNT(*) AS c FROM vdl2_messages "
        "WHERE ts >= ? AND ts < ? GROUP BY b",
        (bucket, bucket, from_ts, to_ts),
    ).fetchall():
        i = idx.get(r["b"])
        if i is not None:
            rate[i] = r["c"]

    cols = {f: [0] * n for f in top}
    if top:
        topset = set(top)
        for r in conn.execute(
            "SELECT (ts / ?) * ? AS b, ROUND(freq, 3) AS f, COUNT(*) AS c FROM vdl2_messages "
            "WHERE freq IS NOT NULL AND ts >= ? AND ts < ? GROUP BY b, f",
            (bucket, bucket, from_ts, to_ts),
        ).fetchall():
            if r["f"] in topset:
                i = idx.get(r["b"])
                if i is not None:
                    cols[r["f"]][i] = r["c"]

    per_min = 60.0 / bucket

    def norm(counts: list[int]) -> list[float]:
        return [round(c * per_min, 2) for c in counts]

    data = [[float(b) for b in buckets], norm(rate)] + [norm(cols[f]) for f in top]
    return {
        "bucket_seconds": bucket,
        "metrics": ["rate"] + [_fmt_freq(f) for f in top],
        "freqs": top,
        "total": total,
        "newest_ts": newest,
        "newest_age_sec": (int(time.time()) - newest) if newest is not None else None,
        "data": data,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_api_vdl2.py -q -k Timeseries`
Expected: PASS (4 tests)

- [ ] **Step 5: Verify the query plan uses the ts index (no full scan)**

Run:
```bash
sqlite3 :memory: "CREATE TABLE vdl2_messages(id INTEGER PRIMARY KEY, ts INTEGER, freq REAL); CREATE INDEX idx_vdl2_ts ON vdl2_messages(ts DESC); EXPLAIN QUERY PLAN SELECT (ts/60)*60 AS b, COUNT(*) c FROM vdl2_messages WHERE ts>=1 AND ts<2 GROUP BY b;"
```
Expected: output contains `SEARCH ... USING INDEX idx_vdl2_ts` (a `USE TEMP B-TREE FOR GROUP BY` line is fine).

- [ ] **Step 6: Commit**

```bash
git add src/readsbstats/api/vdl2.py tests/test_api_vdl2.py
git commit -m "feat(vdl2): /api/vdl2/timeseries bucketed reception series"
```

---

## Task 3: Frontend — `smallMultHeight` helper + timeseries type

**Files:**
- Modify: `frontend/src/pages/metricsCharts.ts`
- Modify: `frontend/src/lib/types.ts`

- [ ] **Step 1: Export a height helper for N sub-panels**

In `frontend/src/pages/metricsCharts.ts`, just below the existing `SMALL_MULT_HEIGHT`/`smallMultGridTop` definitions, add:

```typescript
// Total canvas height for an `n`-row small-multiples chart. The existing
// SMALL_MULT_HEIGHT (280) is exactly smallMultHeight(4) — the signal panel.
export function smallMultHeight(n: number): number {
  const rows = Math.max(n, 1);
  return SMALL_MULT_TITLE_H + (rows - 1) * (SMALL_MULT_TITLE_H + SMALL_MULT_GRID_H) + SMALL_MULT_GRID_H + 24;
}
```

- [ ] **Step 2: Verify the helper matches the existing constant**

Run: `cd frontend && node -e "const t=14,g=50; const h=n=>{const r=Math.max(n,1);return t+(r-1)*(t+g)+g+24}; console.log(h(4)===280 ? 'ok' : 'MISMATCH '+h(4))"`
Expected: `ok`

- [ ] **Step 3: Add the timeseries TS type**

In `frontend/src/lib/types.ts`, add (it extends `MetricsResp` so it can be passed straight to the chart builders). First confirm `MetricsResp` is importable — it's defined in `src/pages/metricsCharts.ts`. Add the interface near the other `Vdl2*` types:

```typescript
// Bucketed VDL2 reception series for the Metrics page's two charts. Extends the
// columnar /api/metrics shape (MetricsResp) so buildPanelOption /
// buildSignalSmallMultiplesOption consume it directly.
export interface Vdl2TimeseriesResp {
  bucket_seconds: number;
  metrics: string[];
  data: number[][];
  freqs: number[];
  total: number;
  newest_ts: number | null;
  newest_age_sec: number | null;
}
```

- [ ] **Step 4: Type-check**

Run: `cd frontend && npx tsc -b 2>&1 | tail -5`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/metricsCharts.ts frontend/src/lib/types.ts
git commit -m "feat(vdl2): smallMultHeight helper + Vdl2TimeseriesResp type"
```

---

## Task 4: Frontend — rebuild the reception card as two charts (TDD)

**Files:**
- Rewrite: `frontend/src/components/metrics/Vdl2ReceptionCard.tsx`
- Modify: `frontend/src/pages/Metrics.tsx` (pass `from`/`to`)
- Rewrite: `frontend/test/vdl2-reception.test.tsx`

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `frontend/test/vdl2-reception.test.tsx` with:

```tsx
/**
 * VDL2 reception card — two range-driven charts (message rate + per-frequency
 * small multiples) with a slim freshness/total header. ECharts is globally
 * mocked to null (jsdom has no canvas), so we assert the chart wrappers + header,
 * not chart internals.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, waitFor, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Vdl2ReceptionCard } from '@/components/metrics/Vdl2ReceptionCard';

const FIXTURE = {
  bucket_seconds: 60,
  metrics: ['rate', '136.725', '136.875'],
  freqs: [136.725, 136.875],
  total: 556,
  newest_ts: 1000,
  newest_age_sec: 8,
  data: [
    [1000, 1060],
    [2, 1],
    [1.5, 0.5],
    [0.5, 0.5],
  ],
};

let fixture: Record<string, unknown> = FIXTURE;
let fetchSpy: ReturnType<typeof vi.fn>;

function stubFetch() {
  fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const body = url.includes('/api/vdl2/timeseries') ? fixture : { ok: true };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  });
  globalThis.fetch = fetchSpy as unknown as typeof fetch;
}

function renderCard(enabled = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <Vdl2ReceptionCard enabled={enabled} from={900} to={1100} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fixture = FIXTURE;
  stubFetch();
});

describe('Vdl2ReceptionCard', () => {
  it('renders both chart wrappers and the header total + freshness', async () => {
    renderCard();
    await waitFor(() => screen.getByTestId('vdl2-rate-chart'));
    expect(screen.getByTestId('vdl2-freq-charts')).toBeTruthy();
    const fresh = screen.getByTestId('vdl2-reception-freshness');
    expect(fresh.textContent).toContain('556');
    expect(fresh.textContent).toContain('8s ago');
    expect(fresh.textContent).not.toContain('⚠');
  });

  it('flags a stale feed', async () => {
    fixture = { ...FIXTURE, newest_age_sec: 1200 };
    renderCard();
    await waitFor(() =>
      expect(screen.getByTestId('vdl2-reception-freshness').textContent).toContain('⚠'),
    );
    expect(screen.getByTestId('vdl2-reception-freshness').className).toContain('color-danger');
  });

  it('renders nothing and makes no request when not enabled', async () => {
    const { container } = renderCard(false);
    await waitFor(() => expect(fetchSpy).not.toHaveBeenCalled());
    expect(screen.queryByTestId('metrics-vdl2-reception')).toBeNull();
    expect(container.firstChild).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npx vitest run test/vdl2-reception.test.tsx`
Expected: FAIL (the current card has no `from`/`to` props / `vdl2-rate-chart` testid)

- [ ] **Step 3: Rewrite the card**

Replace the entire contents of `frontend/src/components/metrics/Vdl2ReceptionCard.tsx` with:

```tsx
import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { EChartsOption } from 'echarts';
import { apiJson } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { EChart } from '@/components/charts/EChart';
import { CHART_COLORS } from '@/components/charts/theme';
import { useFormat } from '@/hooks/useFormat';
import { fmtAgo } from '@/lib/format';
import {
  buildPanelOption,
  buildSignalSmallMultiplesOption,
  smallMultHeight,
  type MetricsResp,
} from '@/pages/metricsCharts';
import type { Vdl2TimeseriesResp } from '@/lib/types';

// A feed quiet for longer than this reads as "stale" — VDL2/ACARS is bursty, so
// keep it lenient to avoid false alarms during genuine quiet spells.
const STALE_SEC = 600;
// Up to six per-frequency sub-panels (matches the backend top-6).
const FREQ_COLORS = [
  CHART_COLORS.orange,
  CHART_COLORS.accent,
  CHART_COLORS.success,
  CHART_COLORS.purple,
  CHART_COLORS.warn,
  CHART_COLORS.danger,
];

function fmtFreshness(ts: number | null, ageSec: number | null): string {
  if (ts == null || ageSec == null) return 'no data';
  // Feed fmtAgo the server-computed age (now = ts + age) so there's no clock skew.
  return fmtAgo(ts, ts + ageSec);
}

// VDL2 reception card: two range-driven ECharts — total message rate (msgs/min)
// and per-frequency small multiples (signal-panel style) — over the Metrics
// page's [from, to] window. vdlm2dec-only; NO signal level. Self-gating: renders
// nothing and makes no request when `enabled` is false.
export function Vdl2ReceptionCard({
  enabled = true,
  from,
  to,
}: {
  enabled?: boolean;
  from: number;
  to: number;
}) {
  const { fmtTs, fmtAxisTime, fmtAxisDate } = useFormat();
  const { data: resp } = useQuery<Vdl2TimeseriesResp>({
    queryKey: ['vdl2-timeseries', from, to],
    enabled,
    queryFn: () => apiJson<Vdl2TimeseriesResp>(`vdl2/timeseries?from=${from}&to=${to}`),
    placeholderData: (prev) => prev,
    staleTime: 30_000,
  });

  const freqKeys = resp?.metrics.slice(1) ?? [];

  const rateOption = useMemo<EChartsOption>(
    () =>
      buildPanelOption(
        resp as MetricsResp | undefined,
        ['rate'],
        [CHART_COLORS.orange],
        fmtAxisTime,
        fmtAxisDate,
        fmtTs,
      ),
    [resp, fmtAxisTime, fmtAxisDate, fmtTs],
  );
  const freqOption = useMemo<EChartsOption>(
    () =>
      buildSignalSmallMultiplesOption(
        resp as MetricsResp | undefined,
        freqKeys,
        FREQ_COLORS,
        freqKeys,
        fmtAxisTime,
        fmtAxisDate,
        fmtTs,
      ),
    [resp, freqKeys, fmtAxisTime, fmtAxisDate, fmtTs],
  );

  if (!enabled) return null;

  const ageSec = resp?.newest_age_sec ?? null;
  const stale = resp != null && (ageSec == null || ageSec > STALE_SEC);

  return (
    <Card data-testid="metrics-vdl2-reception">
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle>VDL2 / ACARS reception</CardTitle>
        <span
          data-testid="vdl2-reception-freshness"
          className={
            stale
              ? 'text-xs font-medium text-[var(--color-danger)]'
              : 'text-xs text-[var(--color-text-dim)]'
          }
        >
          {resp
            ? `${resp.total.toLocaleString()} msgs · ${stale ? '⚠ ' : ''}last ${fmtFreshness(
                resp.newest_ts,
                ageSec,
              )}`
            : '—'}
        </span>
      </CardHeader>
      <CardContent className="space-y-4">
        <div data-testid="vdl2-rate-chart">
          <div className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
            Message rate — msgs/min
          </div>
          <EChart option={rateOption} group="metrics" height={180} />
        </div>
        <div data-testid="vdl2-freq-charts">
          <div className="mb-1 text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]">
            Per-frequency — msgs/min
          </div>
          <EChart
            option={freqOption}
            group="metrics"
            height={smallMultHeight(freqKeys.length)}
          />
        </div>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 4: Pass `from`/`to` from the Metrics page**

In `frontend/src/pages/Metrics.tsx`, find the mount (around line 191):

```tsx
{vdl2Available && <Vdl2ReceptionCard enabled={vdl2Available} />}
```

Replace with:

```tsx
{vdl2Available && <Vdl2ReceptionCard enabled={vdl2Available} from={from} to={to} />}
```

(`from` and `to` already exist in `MetricsPage` — they are the same values passed to `/api/metrics`.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd frontend && npx vitest run test/vdl2-reception.test.tsx`
Expected: PASS (3 tests)

- [ ] **Step 6: Lint + typecheck the touched files**

Run: `cd frontend && npm run lint && npx tsc -b 2>&1 | tail -3`
Expected: lint clean, no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/metrics/Vdl2ReceptionCard.tsx frontend/src/pages/Metrics.tsx frontend/test/vdl2-reception.test.tsx
git commit -m "feat(vdl2): rebuild reception card as two range-driven charts"
```

---

## Task 5: Remove the superseded reception endpoint, schemas, types, tests

**Files:**
- Modify: `src/readsbstats/api/vdl2.py` (remove `api_vdl2_reception` + `_compute_reception`; KEEP `_rate_buckets` — `_compute_stats` still uses it for the hourly trend)
- Modify: `src/readsbstats/schemas.py` (remove `Vdl2ReceptionResponse`, `Vdl2FreqStat`)
- Modify: `tests/test_api_vdl2.py` (remove `class TestReception`; remove the reception 503 assertion line)
- Modify: `frontend/src/lib/types.ts` (remove `Vdl2ReceptionResponse`, `Vdl2FreqStat`)

- [ ] **Step 1: Remove the backend reception endpoint + compute fn**

In `src/readsbstats/api/vdl2.py`, delete the `@router.get("/api/vdl2/reception" ...)` decorated `api_vdl2_reception` function AND the `_compute_reception` function. **Do NOT delete `_rate_buckets`** — confirm it's still referenced:

Run: `grep -n "_rate_buckets\|_compute_reception\|api_vdl2_reception" src/readsbstats/api/vdl2.py`
Expected after deletion: only the `_rate_buckets` definition + its call inside `_compute_stats` remain; no `_compute_reception`/`api_vdl2_reception`.

- [ ] **Step 2: Remove the reception schemas**

In `src/readsbstats/schemas.py`, delete the `Vdl2FreqStat` and `Vdl2ReceptionResponse` classes. Confirm nothing else imports them:

Run: `grep -rn "Vdl2ReceptionResponse\|Vdl2FreqStat" src/`
Expected: no matches.

- [ ] **Step 3: Remove the backend reception tests**

In `tests/test_api_vdl2.py`, delete the whole `class TestReception:` block, and remove this one line from `TestFailureModes.test_endpoints_503_when_db_unavailable`:

```python
            assert c.get("/api/vdl2/reception").status_code == 503
```

- [ ] **Step 4: Remove the frontend reception types**

In `frontend/src/lib/types.ts`, delete the `Vdl2FreqStat` and `Vdl2ReceptionResponse` interfaces. Confirm nothing imports them:

Run: `cd frontend && grep -rn "Vdl2ReceptionResponse\|Vdl2FreqStat" src/`
Expected: no matches.

- [ ] **Step 5: Run the full backend + frontend suites**

Run: `python -m pytest tests/ -q 2>&1 | tail -3`
Expected: all pass (count drops by the removed reception tests, plus the +4 timeseries from Task 2).

Run: `cd frontend && npm test 2>&1 | grep -E "Test Files|Tests " | tail -2 && npm run lint 2>&1 | tail -2 && npm run build 2>&1 | grep -E "built in|error" | tail -1`
Expected: all pass, lint clean, build clean.

- [ ] **Step 6: Commit**

```bash
git add src/readsbstats/api/vdl2.py src/readsbstats/schemas.py tests/test_api_vdl2.py frontend/src/lib/types.ts
git commit -m "refactor(vdl2): remove superseded /api/vdl2/reception (replaced by timeseries)"
```

---

## Task 6: Docs, final verification, deploy

**Files:**
- Modify: `docs/api.md`, `CHANGELOG.md`, `README.md`, `docs/development.md`

- [ ] **Step 1: Update the API doc**

In `docs/api.md`, replace the `/api/vdl2/reception` table row with:

```markdown
| GET | `/api/vdl2/timeseries` | Bucketed reception series for the Metrics charts over `from`/`to` (epoch). Columnar like `/api/metrics`: `{bucket_seconds, metrics:["rate", <freq>…], freqs[], total, newest_ts, newest_age_sec, data:[[ts…],[rate…],…]}`. Values are msgs/min; buckets coarsen with span (60→14400 s); top-6 frequencies by volume; zero-filled. |
```

- [ ] **Step 2: Update CHANGELOG**

In `CHANGELOG.md`, under the v2.15.0 entry, add a bullet (and refresh the test counts to the real numbers from Step 4):

```markdown
- **VDL2 reception redesign** — the Metrics card is now two range-driven ECharts (message
  rate + per-frequency small multiples, dBFS-panel style) sharing the page's range picker,
  fed by a new `GET /api/vdl2/timeseries`. Replaces the KPI-tile card + `/api/vdl2/reception`.
```

- [ ] **Step 3: Run the full suites and capture counts**

Run: `python -m pytest tests/ -q 2>&1 | tail -1`
Run: `cd frontend && npm test 2>&1 | grep "Tests " | tail -1`

- [ ] **Step 4: Update test counts**

In `README.md` and `docs/development.md`, set the pytest and Vitest counts to the numbers from Step 3. In `CHANGELOG.md` update the "Tests: NNNN Python, NNN Vitest" line.

- [ ] **Step 5: Local end-to-end smoke**

Run (seed a couple rows first if needed):
```bash
RSBS_VDL2_ENABLED=true RSBS_VDL2_DB_PATH=./db/vdl2.db RSBS_DB_PATH=~/projects/readsbstats/db/history.db RSBS_ROOT_PATH="" \
  uvicorn readsbstats.web:app --host 127.0.0.1 --port 8080 &
sleep 3
curl -s "http://127.0.0.1:8080/api/vdl2/timeseries?from=$(($(date +%s)-86400))&to=$(date +%s)" | python3 -m json.tool | head -20
kill %1
```
Expected: a JSON object with `bucket_seconds: 60`, a `rate` series, and per-freq series.

- [ ] **Step 6: Commit docs**

```bash
git add docs/api.md CHANGELOG.md README.md docs/development.md
git commit -m "docs(vdl2): document /api/vdl2/timeseries; refresh test counts"
```

- [ ] **Step 7: Push + (optional) deploy to the Pi**

```bash
git push
```
Then deploy via the `deploy-to-pi` skill (autonomous, including the now-NOPASSWD `vdlm2dec` restart). On the Pi, open Metrics and confirm the two VDL2 charts render and move with the range picker (24h / 7d).

---

## Self-review notes (author)

- **Spec coverage:** endpoint shape + bucketing + msgs/min + zero-fill + dynamic top-6 (Task 2); two charts reusing builders + shared range picker + slim header (Task 4); removals (Task 5); `.gitignore` for `.superpowers/` was committed with the spec. ✓
- **`_rate_buckets` retained** — it is shared with `_compute_stats` (hourly trend); only `_compute_reception` is removed. ✓
- **Freshness source:** implemented via the timeseries response's `newest_ts`/`newest_age_sec` (table-wide `MAX(ts)`), self-contained — a minor refinement of the spec's "from the health query" that avoids a hook change while keeping the same freshness UX. ✓
- **Type consistency:** `Vdl2TimeseriesResp` (frontend) mirrors `Vdl2TimeseriesResponse` (backend); chart builders called with the exact signatures verified in `metricsCharts.ts`. ✓
