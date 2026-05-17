# Changelog

## 2.1.12 — 2026-05-17

### Security — CodeQL #29 (py/url-redirection) defence-in-depth

CodeQL alert
[#29](https://github.com/blindp3w/readsbstats/security/code-scanning/29)
flagged the `/v2/{rest:path}` redirect handler with the same
`py/url-redirection` rule that produced #28 in v2.1.1. The previous fix
added a `_sanitize_v2_rest` custom sanitizer that strips leading `/` /
`\\` / CR / LF and percent-encodes URL-special characters — functionally
correct, but CodeQL's data-flow analyzer cannot statically recognise our
custom helper as a safe sanitizer, so it kept flagging the path from
the `rest` parameter into `RedirectResponse(url=target)`.

This release adds the recognized sanitizer pattern from CodeQL's own
documentation: an `urllib.parse.urlparse(target)` check that the final
redirect target has neither a scheme nor a netloc. If anything slips
past `_sanitize_v2_rest` (defence-in-depth — should never happen given
the existing strips), the handler falls back to redirecting to the SPA
root instead of honouring the off-site target.

```python
parsed_target = urllib.parse.urlparse(target)
if parsed_target.scheme or parsed_target.netloc:
    return RedirectResponse(url=f"{root}/", status_code=301)
return RedirectResponse(url=target, status_code=301)
```

Same shape as the example in
https://codeql.github.com/codeql-query-help/python/py-url-redirection/
— ensures CodeQL recognises the guard.

**Test**: `tests/test_web.py::TestSpaMount::test_v2_compat_urlparse_guard_falls_back_to_root`
— monkey-patches `_sanitize_v2_rest` to a deliberately broken version
that returns `/evil.com`, then verifies the route still produces a
same-origin redirect rather than honouring the off-site target.

**Test totals**: Python 1316 → 1317 (+1). Vitest 90 (unchanged).

## 2.1.11 — 2026-05-17

### Audit 12 Phase 9 — DNS-rebinding fix redesigned (H1 + H2)

The Phase 2 (v2.1.4) DNS-rebinding TOCTOU fix worked but was brittle: it
installed a process-wide ``socket.getaddrinfo`` patch at module load that
checked a thread-local pin. Any test doing the obvious
``monkeypatch.setattr(socket, "getaddrinfo", ...)`` was silently no-op'd,
and the design wouldn't naturally cover ``httpx.AsyncClient`` (which
bypasses ``socket.getaddrinfo`` via ``anyio.getaddrinfo``).

Phase 9 eliminates the global patch entirely. Two distinct mechanisms
now close the TOCTOU per code path:

**urllib path — custom HTTPSConnection (audit-12 H1)**

`safe_urlopen` now builds a one-shot opener per call (via
`_build_pinned_opener`) whose HTTPS handler issues every connection
through a new `_PinnedHTTPSConnection`. The connection:

- Connects to the pre-validated IP directly via `socket.create_connection`
  — no DNS lookup happens between `validate_url` and the connect.
- TLS handshake uses the original hostname for SNI AND triggers Python's
  standard hostname-vs-cert verification.
- urllib's `Host:` header is set automatically from the URL host.

No reliance on `socket.getaddrinfo` at all between resolve+validate and
the actual fetch. The rebinding window is closed at the protocol layer.

**httpx path — scoped resolver redirect (audit-12 H1 partial)**

`safe_httpx_get` wraps the call in `_pinned_socket_resolver`, a
`@contextmanager` that temporarily redirects `socket.getaddrinfo` to
return the pre-validated info tuple for the duration of the single
request, then restores the original in `finally`. No module-load global
patch; the redirection is fully scoped to one fetch.

The redirection is technically still process-wide for the brief window
inside the `with` block — but unlike Phase 2's permanent patch, tests
patching `socket.getaddrinfo` outside this narrow window now behave as
expected. The trade-off is documented at the top of `http_safe.py`.

**Async-httpx guard (audit-12 H2)**

`safe_httpx_get` now raises `RuntimeError` immediately if passed an
`httpx.AsyncClient`. Async httpx bypasses `socket.getaddrinfo` via
`anyio.getaddrinfo`, so our scoped pin doesn't protect it. We don't
currently use async httpx anywhere; the guard is defensive against
future drift. If you ever need async support, implement a custom
`httpcore.NetworkBackend` with the resolution baked in.

**Code-shape changes**

Removed (module-level globals):

- `_dns_pin` (thread-local pin storage)
- `_pinned_getaddrinfo` (the global wrapper)
- `_set_dns_pin`, `_clear_dns_pin` (pin lifecycle helpers)
- `_no_redirect_opener` (the module-level opener — replaced by
  per-call `_build_pinned_opener`)
- `socket.getaddrinfo = _pinned_getaddrinfo` (the module-load patch)

Added:

- `_PinnedHTTPSConnection` — `http.client.HTTPSConnection` subclass
  that connects to a pre-validated IP with proper SNI.
- `_PinnedHTTPSHandler` — `urllib.request.HTTPSHandler` factory using
  the connection.
- `_build_pinned_opener(parsed, target_ip, timeout)` — one-shot
  opener builder.
- `_pinned_socket_resolver(hostname, infos)` — scoped resolver
  context-manager for the httpx path.
- `_resolve_and_validate(url) -> (parsed, infos)` — the shared
  resolve+validate helper used by both code paths.

Kept (back-compat):

- `validate_url(url)` — still public, still validates URLs, but now
  discards the addrinfo (callers that want to fetch should use
  `safe_urlopen` / `safe_httpx_get` which do their own resolution).
- `_real_getaddrinfo` — captured `socket.getaddrinfo` reference, kept
  so tests can monkey-patch resolution without fighting the missing
  global patch.

**Tests**

- New `TestUrllibPinnedConnection` (3 cases) — verifies
  `_PinnedHTTPSConnection` is constructed with the right IP, both
  handlers wired into the opener, the resolve helper returns infos.
- New `TestHttpxScopedResolver` (3 cases) — verifies the resolver
  redirect only applies inside the `with` block, restores on
  exception, falls through for other hosts.
- New `TestHttpxAsyncRejection` (1 case) — `AsyncClient` raises
  `RuntimeError`.
- `TestSafeUrlopen` rewritten — now mocks `_build_pinned_opener`
  factory instead of the deleted `_no_redirect_opener`.
- `test_photo_sources.py`, `test_db_updater.py` — monkey-patches
  updated to the new surface.

Two old tests removed (no longer applicable): the `_no_redirect_opener`
wiring guard (replaced by `test_build_pinned_opener_wires_both_handlers`)
and four `TestDnsPinning` tests for the removed thread-local pin
behaviour (replaced by the new redesigned-path tests).

**Test totals**: Python 1314 → 1316 (+2 net). Vitest 90 (unchanged).
Frontend `npm run build` clean.

**This closes Audit 12.** All High-severity findings now have proper
fixes. The three large refactors (#193 web.py split, #194 _migrate
split, #195 page extractions) and a handful of Low-severity cosmetics
remain as opportunistic future work, but no further phases planned.

## 2.1.10 — 2026-05-17

### Audit 12 Phase 8 — self-review follow-up

Three parallel review agents went through every Audit 12 change. Phase 8
ships the actionable findings from that review. No new functionality;
small bug fixes, defence-in-depth additions, and documentation /
test-correctness fixes.

**Security defence-in-depth**

- **#149 P8** `_v2_compat` sanitizer now `urllib.parse.quote(rest, safe="/")`
  on top of the CR/LF strip — the audit's original recommendation
  included this step but only the strip shipped in v2.1.4. A path with
  literal spaces / quotes / `?` / `#` characters now produces a
  well-formed (percent-encoded) Location header instead of a potentially
  malformed one.
- **#171 P8** `/api/settings` `stats_json` label stopped hardcoding
  `/run/readsb/stats.json` as the "default" sentinel. The comparison
  duplicated the default from `config.py` (drift-prone) and leaked one
  bit ("did the operator customise this path"). Now uniformly reports
  `(configured)` or `(not set)`.

**Reliability**

- **#154 P8** `_watch_remove` now falls back to `callsign_prefix` as the
  third match-type. The usage string promised all three (`icao` /
  `registration` / `callsign_prefix`) but the fallback chain only tried
  the first two. Telegram-bot users could not remove a callsign_prefix
  entry without using the HTTP API.

**Type safety / data quality**

- **#P8** `lib/types.ts::WatchlistEntry.created_at` and `airborne`
  marked optional. The `GET /api/watchlist` endpoint returns all six
  fields; `POST /api/watchlist` returns only four. Previously the type
  declared all six as required which would surface as silent
  `undefined`s if anyone read those fields off a mutation result.

**Test correctness**

- **#P8** `frontend/test/smoke.test.tsx` had stub shapes that didn't
  match the real response interfaces — pages rendered the empty path by
  accident rather than by exercising the actual code path. Stubs for
  `/api/metrics`, `/api/metrics/health`, and `/api/flights` corrected to
  the real `MetricsResp` / `HealthResp` / `FlightsResponse` shapes.

**Dead code / style cleanup**

- **#P6 follow-up** Removed two duplicate `import re` / `from . import
  database` inside `notifier._watch_add` and `notifier._listener_loop`
  that should have been caught in Phase 6's "imports at module top"
  sweep. (`database` is already imported at module top; the "circular
  dependency" comment in `_listener_loop` was historical and no longer
  applies — verified by grep.)
- **#P6 follow-up** Moved the `from _purge_helpers import` line in both
  purge scripts to the top import block. Moved `from collections
  import OrderedDict as _OrderedDict` in `web.py` to the top.
- **#P6 follow-up** `.claude/rules/python.md` referenced the old
  `_clamp_int` / `_clamp_float` names — renamed to
  `_min_or_default_int` / `_min_or_default_float` to match the actual
  function names. Also documents `_bool()` as the canonical boolean
  env-var parser.

**Doc clarity**

- Three `apply_purge()` docstrings now document the batched-commit
  semantics introduced in Phase 3. The old docstrings claimed atomicity
  that no longer holds; the new text explicitly notes that an
  interrupted run can leave the DB partially purged, and that the
  script is idempotent so re-running finishes the work.
- **#197 P8** CHANGELOG entry for v2.1.8 now explicitly notes the
  case-sensitivity behaviour change in `_bool` (two flags previously
  treated `False` as truthy because of case-sensitive comparison).

**Test deltas**

- `tests/test_web.py`: added 1 new test for the `_v2_compat` quote step
  + 1 updated test for the new CR/LF-then-quote ordering + 1 updated
  assertion for the new `stats_json` label.
- `tests/test_notifier.py`: added 1 new test for the
  `callsign_prefix` fallback.

**Test totals**: Python 1312 → 1314 (+2). Vitest 90 (unchanged).

**Other items from the self-review intentionally not addressed in this
phase**:

- **H1 / H2** (DNS-pin scope is too broad and doesn't reliably cover
  async httpx) — would require a per-transport resolver hook, a
  multi-file refactor of `http_safe.py`. Worth its own dedicated PR.
- **M8** (three new tests reach across global state in fragile ways) —
  works today, would surface as flakes only under `pytest-xdist` or
  reordering. Defer to the next time someone actually wants parallel
  test execution.
- **M1** (purge scripts now non-atomic) — addressed via docstring
  updates rather than restoring atomicity; the Phase 3 trade-off
  (lock-starvation avoidance) is the right call for the actual
  workload.

## 2.1.9 — 2026-05-17

### Audit 12 Phase 7 — documentation hygiene

Doc-only. No source-code behavior change. Fixes stale references to
files/paths/identifiers that didn't survive v2.0.0 or the audit work.

- `src/readsbstats/http_safe.py` module docstring referenced
  `static/js/table-utils.js` — replaced with the current
  `frontend/src/lib/safeUrl.ts` path.
- `frontend/src/lib/safeUrl.ts` opening comment said "Ported from
  static/js/table-utils.js:safeHttpUrl" with no acknowledgement that
  the file is deleted — clarified.
- `frontend/src/main.tsx` basename comment described the
  v2.0.0-rc.1 transitional `/stats/v2/` prefix instead of the
  current canonical `/stats/`.
- `frontend/CLAUDE.md` claimed "Vitest + jsdom (43 tests)" — updated
  to the post-audit count.

No tests change.

This is the final phase of Audit 12. Across six shipped phases
(v2.1.3 → v2.1.8) plus this doc cleanup, **~60 of ~75 numbered
audit findings** are closed. Remaining items are three large
deferred refactors (#193 web.py split, #194 _migrate() split,
#195 page extractions) plus a handful of Low-severity cosmetic
items, all tracked in `internal_docs/security/audit-12-2026-05-17-post-v2.md`.

## 2.1.8 — 2026-05-17

### Audit 12 Phase 6 — style + dead-code cleanup

Refactor-only phase. No behavior change. Closes the "smaller" audit
items: dead code deletions, duplicated definitions consolidated,
inconsistent env parsing unified, and a few stale names corrected.

**Dead code removed**

- `frontend/src/pages/Hello.tsx` — Phase 0 PoC, never routed.
- `metrics_collector._g` — helper defined but never called.
- `route_enricher._is_confirmed_unknown` + its 3 tests — only used in
  tests, no production caller.

**Module-top imports**

- `web.py` `import re` was inside `_feeder_details_mlat` — moved to top.
- `db_updater.py` `from . import http_safe` was inside `_fetch` —
  promoted to the module-level import block.
- `scripts/import_rrd.py` `from datetime import ...` was inside a loop
  — moved to module top.

**De-duplication**

- **#197** Centralised boolean env parsing in `config._bool(name, default)`.
  Replaced five inconsistent `os.getenv(...) not in (...)` patterns
  (`WIKIPEDIA_PHOTO`, `ADSBX_ENABLED`, `METRICS_ENABLED`, `USE_DUCKDB`,
  `PREWARM_MAP_CACHE`) that had drifted in their tuple ordering and
  empty-string handling. 13 new tests pin the contract.

  **⚠ Minor behaviour change:** the new helper case-normalises before
  comparing, where two of the old patterns (`ADSBX_ENABLED`,
  `PREWARM_MAP_CACHE`) did not. Operators who had `RSBS_ADSBX_ENABLED=False`
  (capital F) — previously treated as truthy because `"False" != "false"`
  — will now see those flags correctly recognised as falsy. The fix is
  to use `0` (or lowercase `false`/`no`/`off`) — the documented falsy
  values. Audit-12 Phase 8 follow-up documented this explicitly.
- **#198** `_TransientError` was declared identically in 3 modules
  (`route_enricher`, `adsbx_enricher`, `metrics_collector`). Now a
  single `http_safe.TransientError` aliased into each consumer; tests
  still resolve `<module>._TransientError` so no test churn.
- **#199** `_new_max_gs` was duplicated in `purge_bad_gs` and
  `purge_mlat_gs_spikes`. Extracted to new `scripts/_purge_helpers.py`
  with both scripts importing the canonical version.

**Renames for clarity**

- **#196** `_clamp_int` / `_clamp_float` → `_min_or_default_int` /
  `_min_or_default_float`. The helpers only enforce a lower bound;
  "clamp" implied two-sided clamping. Docstrings updated to be
  explicit.
- **#P6.6** `components/ui/Input.tsx::Select` → `NativeSelect`.
  Disambiguates from the Radix `Select` in `@/components/ui/Select`
  (the styled-dropdown primitive).
- **#P6.7** `WatchlistEntry` type unified in new
  `frontend/src/lib/types.ts`. Was declared in two places
  (`Aircraft.tsx`, `Watchlist.tsx`) with divergent shapes.

**Test suite**: Python 1299 → 1312 (+13). Vitest 90 (unchanged).

**Deferred to a later release** (too large for a single phase):
- `web.py` 2535-line file split into `routes/` + `prewarm.py` + etc. (#193)
- `database._migrate()` 170-line monolith split into focused helpers (#194)
- Page extractions (Stats/Map/Flight/Metrics over 300 lines) (#195)

## 2.1.7 — 2026-05-17

### Audit 12 Phase 5 — test coverage hardening

Test-only phase. No production code changes. Closes the most-leverage
coverage gaps the audit flagged so future refactors trip a test before
reaching prod.

**Frontend (Vitest 54 → 90, +36 tests)**

- **#200** `useSearchParamBatch` covered for the first time. 12 tests
  exercise single-param and multi-param updates, default-stripping,
  explicit `null` removal, and the documented contract that the helper
  is the v7 stale-state *fix* (one call with multiple keys), not a
  workaround for two-call usage. CLAUDE.md flagged this as the
  highest-leverage missing test.
- **#210** `lib/flags.ts` covered with 12 tests: FLAG_* bit values pinned
  to backend `config.py`, the `primaryFlagLabel` precedence ladder
  (military > interesting > anonymous > none), and PIA/LADD non-
  surfacing as primary labels.
- **#201** Smoke tests for App shell + ErrorBoundary + 9 pages
  (Settings, Feeders, Watchlist, History, Gallery, Stats, Metrics,
  Aircraft, Flight). Shared QueryClient + MemoryRouter + global-fetch
  stub harness. Each test verifies "renders without throwing on
  minimal/empty data" so a regression in imports, required props, or
  initial-state assumptions surfaces in CI. `Map.tsx` skipped
  (Leaflet's imperative DOM mutation isn't fully shimmed in jsdom;
  Playwright mobile suite covers it). `Hello.tsx` skipped (PoC, not
  routed).

**Backend (Python 1252 → 1299, +47 tests)**

- **#212** `notifier._h()` — direct unit tests for the HTML escape
  primitive (was indirectly covered via `notify_*`).
- **#206** `http_safe` IPv6 reject branches: loopback (`::1`),
  link-local (`fe80::`), unique-local (`fc00::/7`), multicast
  (`ff00::/8`), unspecified (`::`). Plus the previously-uncovered
  IPv4 `0.0.0.0`, RFC1918 (10/8, 172.16/12, 192.168/16), and a
  "mixed addrinfo with one private result rejects the whole URL"
  rebinding-defence test.
- **#204** `country_sql_case` — the SQL twin of `icao_to_country` had
  no direct tests. Added parity vs. Python for diverse hexes, the
  apostrophe-escape contract (`'` → `''`), and a synthetic execute
  check for hypothetical apostrophe-bearing country names.
- **#205** `_RAW` boundary edges parametrised for 6 representative
  blocks: exact start + exact end include, start-1 / end+1 fall out
  to a neighbour, and a "no partial overlap in _RAW" structural
  invariant.
- **#208** `analytics` engine-init error branches: unsafe DB_PATH
  rejects, OSError on `mkdir`, DuckDB exception during INSTALL/LOAD/
  ATTACH. Plus per-query exception → None fallback for both
  `heatmap` and `coverage`, and `close()` resets `_CONN`.
- **#211** `_prewarm_loop` survives one `_prewarm_one` raising —
  loop catches, schedules backoff for that target, continues with
  the next.
- **#203** `purge_mlat_gs_spikes` `TestMain` class — dry-run report,
  --apply modifies data, no-spikes-clean message, snapshot-by-default
  on --apply.

## 2.1.6 — 2026-05-17

### Audit 12 Phase 4 — performance + UX polish

Performance + UX cleanup phase. No new features; reduces a long-running
data backfill from O(n²) to O(n), spreads prewarmer startup CPU across
~100s instead of bunching it at boot, bounds three previously-unbounded
collections, prevents an in-memory hammering loop during upstream
outages, and tightens a handful of UI rough edges.

**Backend performance**

- **#147** `database.backfill_bearing` now uses a `WHERE id > last_id`
  cursor pattern. Each row is examined exactly once; the previous
  LIMIT-subquery pattern re-scanned the table from the top on every
  iteration (O(n²)). On a Pi 4 with 200k+ flights this turns hours of
  backfill into ~30s.
- **#185** Prewarmer no longer starts all 8 cache targets at `next_at=0.0`
  (which caused 8 back-to-back full-table scans across the first ~80s
  of startup). New `_initial_prewarm_schedule()` helper staggers the
  first refresh of each target by 15s, ordered by TTL ascending so
  the shortest-window heatmap/coverage (most-likely-hit by a user) is
  warmed first.

**Bounded memory**

- **#150** `web._type_fetch_locks` is now an LRU-bounded `OrderedDict`
  capped at 1024 entries. ICAO type designators are ~3k distinct so
  the cap is comfortable headroom; the previous unbounded dict would
  grow across worker lifetime without bound.
- **#186** `collector._squawk_notified` now gets `discard(flight_id)` in
  `_close_flight`, so the set is naturally bounded by max-concurrent
  active flights (a few thousand) rather than growing forever.
  `_notified_icao` is intentionally left unbounded — bounded LRU
  semantics would re-alert for the oldest first-sighting ICAOs after
  wraparound, which is the wrong behaviour for that data shape. The
  set is bounded in practice by tens of thousands of distinct flagged
  ICAOs (<50MB resident) over years of operation. A comment now
  documents this.

**Reliability**

- **#155** `route_enricher` now keeps a per-callsign cooldown after a
  transient (network / HTTP) failure. Without it, a multi-hour
  upstream outage hammered the same N callsigns every batch interval.
  Default cooldown is 300s; success clears the cooldown entry so
  recovered state isn't sticky.
- **#154** `notifier._watch_remove` now tries the inferred match_type
  first (preserves Audit 11 #116 — when both icao and registration
  rows exist for the same 6-hex value, icao wins). If that lookup
  matches nothing, falls back to the alternate type so a 6-hex-shaped
  registration (e.g. `ABC123`) is still removable via the bot.

**Frontend UX**

- **#157** `Map.tsx`: deleted dead `tickRef` state. Consolidated three
  duplicate comment blocks about the `<input type="range">` quirk into
  one coherent paragraph.
- **#158** `Map.tsx`: when a rewind snapshot fetch fails, the previous
  moment's data stays visible (intentional — avoids flicker). Now
  shows an inline "stale" badge on the snapshot timestamp pill so the
  user knows the displayed time is not the requested time.
- **#159** `Metrics.tsx`: hoisted `ALL_METRICS` from `useMemo(..., [])`
  to module scope. PANELS is a module constant so the join is
  constant — computing it once at import time is simpler and avoids
  the future-bug shape of a memo silently freezing on first render if
  PANELS ever became dynamic.
- **#160** `Watchlist.tsx`: tightened the optimistic-delete context via
  the 4th `useMutation` generic. `onError`'s `ctx` is now typed
  `DelMutCtx` automatically instead of a hand-typed annotation that
  would drift if `onMutate`'s return shape changed.

**Test suite**: Python 1245 → 1252 (+7). Frontend Vitest unchanged at 54.

## 2.1.5 — 2026-05-17

### Audit 12 Phase 3 — reliability fixes

Phase 3 of the post-v2 audit. Prevents silent outages: stale DB handles,
dropped alerts at shutdown, leaked subprocesses, silently-swallowed
feeder errors, slow/contended SQLite pragmas on a fresh photo connection,
and PK pollution in `adsbx_overrides`. All test-first.

**Metrics collector**

- **#142** `run_metrics_loop` now catches `sqlite3.OperationalError`,
  closes the bad handle, and re-connects via `database.connect()` so a
  moved DB / disk error / WAL hiccup doesn't wedge the collector for
  the rest of the process lifetime.
- **#148** Apply exponential backoff (capped at 300s) on the broad
  `Exception` path too — previously only `_TransientError` triggered
  backoff and a persistent DB error would tight-loop the log at the
  configured 60s interval.

**Purge scripts — batched commits**

- All three `apply_purge()` functions
  (`scripts/purge_ghosts.py`, `scripts/purge_bad_gs.py`,
  `scripts/purge_mlat_gs_spikes.py`) now commit every `_BATCH_SIZE = 100`
  flights instead of wrapping the whole flight loop in one transaction.
  On a database with thousands of flagged flights, the old pattern held
  the SQLite write lock for the full run and starved the collector. A
  single flight's delete + flight-row update still lives in one
  transaction; only the batch boundary commits early.

**Notification queue — drain on shutdown**

- **#145** New `collector.stop_notification_consumer(timeout=5.0)`
  helper drains the queue, posts the `None` sentinel, and joins the
  `tg-dispatch` thread. `main()` calls it during shutdown, after
  finalising active flights, before closing the DB. Previously the
  consumer was a daemon thread the interpreter killed abruptly at
  process exit, dropping any Telegram alerts queued by the last
  `_poll()` before SIGTERM.

**Subprocess leak**

- **#152** `_check_systemd_unit` and `_feeder_details_mlat` now
  `proc.kill()` + `await proc.wait()` on `asyncio.TimeoutError` so a
  hung systemctl/journalctl doesn't pile up zombie children under load.

**Operator visibility**

- **#151** `_fetch_feeder_details` no longer silently returns `[]` on
  exception — logs `WARNING ... exc_info=True` with the feeder name +
  status_type. A misconfigured feeder or corrupted status file would
  otherwise have been invisible.

**Photo lookup connection hygiene**

- **#153** `notifier._get_photo_result` now opens its fresh fallback
  connection via `database.connect()` instead of a bare
  `sqlite3.connect()`. Picks up the project's WAL / synchronous=NORMAL
  / mmap / busy_timeout pragmas — faster writes and 30s busy_timeout
  for collector contention.

**Data quality**

- **#156** `adsbx_enricher._parse_area_response` rejects any `hex` field
  that isn't exactly 6 lowercase hex chars (via new `_is_valid_icao_hex`
  helper). Prevents `~abcdef`-style anonymous-prefix strings and other
  malformed values from polluting the `adsbx_overrides` PK column.

**Test suite**: Python 1232 → 1245 (+13). Vitest unchanged.

## 2.1.4 — 2026-05-17

### Audit 12 Phase 2 — security hardening

Phase 2 of the post-v2 audit. Real-world risk reduction across SSRF,
SQL injection, info disclosure, and XSS surface area. All test-first.

**SSRF guard (http_safe)**

- **#167 + #168** Closed the DNS-rebinding TOCTOU in `safe_urlopen` and
  `safe_httpx_get`. `validate_url()` now resolves DNS via a captured
  `_real_getaddrinfo` and pins the validated infos in thread-local
  storage; a process-wide `socket.getaddrinfo` wrapper returns the
  pinned values for the same host within the same thread, so the
  subsequent fetch resolves to the same IPs we just verified. Other
  threads and other hostnames fall through to the real resolver
  unchanged. Pin is cleared in `finally` after every fetch.

**SQL injection defence-in-depth**

- **#169** Added `_BASELINE_ALLOWED_COLS = {"messages", "signal",
  "ac_with_pos"}` allowlist + `if column not in ...: raise ValueError`
  guards at the entry to `_baseline_avg` and `_recent_avg` in
  `health.py`. Both functions interpolate the column name into SQL via
  f-string; current callers pass literals, but the explicit boundary
  blocks the SQL-injection sink a future caller could otherwise open.

**Info disclosure (`/api/settings`)**

- **#171** Dropped `web_host` and `web_port` from the payload (the
  client is already at that URL; on a reverse-proxied deploy the bind
  host `0.0.0.0` would be misleading anyway). Masked filesystem paths:
  `airspace_geojson` and `stats_json` now return `(set)` / `(default)`
  / `(bundled poland.geojson)` rather than the actual paths. Frontend
  Settings page updated to match. Field names stay so the UI continues
  to show "configured".

**Frontend XSS surface**

- **#174** `react/no-danger` was declared in `eslint.config.mjs` but
  `eslint-plugin-react` was never loaded — the rule was silently
  inactive. Replaced with a Vitest grep test
  (`frontend/test/no-danger.test.ts`) that uses `import.meta.glob` to
  scan every `src/**/*.{ts,tsx}` source file at test time and fails
  the run if any of them contain `dangerouslySetInnerHTML`. Lighter
  than adding the eslint plugin; matches the project's "keep deps
  tight" stance.
- **#176** Hard-coerced `track` in `lib/aircraftIcon.ts`:
  `Math.round(Number.isFinite(Number(track)) ? Number(track) : 0)`
  before interpolating it into the `transform:rotate(${deg}deg)`
  inline style attribute. TypeScript declares `track` as `number |
  null | undefined`, but API drift or a hostile feed could land a
  string here — the coercion ensures CSS injection is impossible
  even under that scenario. Also fixes `isMilitary` to be a true
  boolean rather than a bitwise int.

**Open redirect / response splitting defence-in-depth**

- **#149** Extracted `_sanitize_v2_rest(rest)` helper from
  `_v2_compat` and added CR/LF scrubbing on top of the existing
  leading-`/`/`\\` strip. Starlette rejects raw CR/LF in path
  components today, but the helper is now unit-testable and the
  scrub provides belt-and-braces if a future ASGI server weakens
  that.

**Failure-notification reliability**

- **#166** `scripts/notify-telegram-failure.sh` now guards
  `RSBS_TELEGRAM_TOKEN` and `RSBS_TELEGRAM_CHAT_ID` with `:?` parameter
  expansion at the top of the script. Without it, `set -u` would still
  trip but deeper in the script, producing a confusing journal log
  with no actionable error.

**Test suite**: Python 1218 → 1232 (+14); Vitest 44 → 54 (+10). Both
suites green, frontend builds clean.

## 2.1.3 — 2026-05-17

### Audit 12 Phase 1 — invariant violations + latent footguns

Full post-v2 security/quality audit ran today (`internal_docs/security/audit-12-2026-05-17-post-v2.md`).
This release ships Phase 1 of the proposed sequence: documented-invariant breaks,
one user-visible data-loss bug, and latent footguns. Every fix is test-first; no
functional change for users beyond the `category` data quality improvement.

**Database**

- **#139** Moved the closed-flight `primary_source` backfill out of `_migrate()`
  into a new `_backfill_primary_source()` helper that runs in
  `run_background_migrations()`. The full-table UPDATE was holding the SQLite
  write lock during web startup, violating the documented
  "_migrate is web-hot-path; slow ops go to run_background_migrations" invariant.
- **#140** Added `CREATE TABLE IF NOT EXISTS` for `airports` and `callsign_routes`
  to `_migrate()`. Both tables existed in the DDL (collector path) but were
  missing from `_migrate()` (web path), so a web-only restart against an old
  DB would leave route_enricher writes and airport joins failing until the
  collector restarted.

**Collector**

- **#146** `_poll` now treats an explicit `seen_pos: null` (or missing field) as
  "stale → skip" rather than letting `None > MAX_SEEN_POS_SEC` raise a
  TypeError that the outer `except Exception` swallowed by aborting the whole
  poll cycle. One malformed aircraft entry no longer drops every other aircraft
  for that tick.
- **#144** `_update_flight_agg` now persists `category` via `COALESCE(category, ?)`.
  Previously `category` was set on the first INSERT only; readsb often emits
  it after the first position, leaving the flights row permanently NULL on
  category even though the data was available mid-flight.

**Enrichment cache**

- **#141** Added `_LRUDict.invalidate(key)` and `.clear_locked()` public methods.
  `enrichment.invalidate_adsbx` / `clear_cache` now use them instead of reaching
  into `_LRUDict._lock` / `.pop()` / `.clear()` directly. The previous pattern
  violated the documented "always use get_cached()/put()" contract from
  `src/readsbstats/CLAUDE.md`.

**Purge scripts**

- **#143 + #164** Guarded the empty-list `IN ()` SQL path in
  `purge_ghosts.max_distance_after_purge`, `purge_bad_gs._new_max_gs`, and
  `purge_mlat_gs_spikes._new_max_gs`. SQLite actually accepts `NOT IN ()`
  (audit was wrong about a crash), but standard SQL forbids it — same
  portability concern raised in Audit 11 #118.

**Test suite**: 1200 → 1214 (added 14 regression tests across
`test_database`, `test_purge_*`, `test_enrichment`, `test_collector`).

## 2.1.2 — 2026-05-17

### Docs — restructured documentation tree

Split the monolithic README into purpose-specific guides under `docs/`:

| New file | Contents |
|---|---|
| [`docs/configuration.md`](docs/configuration.md) | All 38 `RSBS_*` environment variables |
| [`docs/api.md`](docs/api.md) | All API endpoints, SPA routes, database schema |
| [`docs/integrations.md`](docs/integrations.md) | Telegram setup, bot commands, ghost/GS filtering |
| [`docs/operations.md`](docs/operations.md) | Updating, useful commands, backups |
| [`docs/development.md`](docs/development.md) | Local dev, tests, build, deploy |
| [`docs/decisions/`](docs/decisions/) | Architecture Decision Records (ADR 0001–0006) |

`README.md` trimmed from 697 to ~270 lines — installation and feature overview only; detailed
reference lives in the guides above.

`docs/piaware_install_ubuntu_24.04_arm64.md` removed (personal setup note, not a readsbstats
guide).

## 2.1.1 — 2026-05-17

### Security — open-redirect in /v2 compat handler (CodeQL py/url-redirection)

CodeQL alert
[#28](https://github.com/blindp3w/readsbstats/security/code-scanning/28)
flagged the `_v2_compat` route in `web.py` (introduced for v2.0.0-rc.1
bookmark compatibility): the captured `rest:path` segment was
interpolated into the Location header without sanitisation. A crafted
request to `/v2//evil.com` produced `Location: //evil.com`, which
browsers treat as a scheme-relative URL and follow off-site —
classical CWE-601 open redirect / phishing vector.

**Exploitable** only when `RSBS_ROOT_PATH=""` (dev mode, the `tests/ui/`
ASGI wrapper, or any deploy without nginx's `/stats` prefix). The
production setup uses `root_path=/stats` so the Location always starts
with `/stats/…` and the scheme-relative smuggle never lands. Fixed
anyway as defence in depth and to silence the static-analysis alert.

**Fix**: `rest.lstrip("/\\")` before constructing the target. Strips
both leading forward and back slashes (some browsers treat `\` as `/`
in URLs per the CodeQL guidance). One-line change in `web.py`. New
test `tests/test_web.py::TestSpaMount::test_v2_open_redirect_blocked`
covers five hostile inputs (`/v2//evil.com`, `/v2///evil.com`,
`/v2/\evil.com`, `/v2/\\evil.com`, `/v2//\evil.com`) and explicitly
sets `app.root_path=""` so the assertion fires against the actual
vulnerability shape, not the prod-config-shielded one.

## 2.1.0 — 2026-05-17

### Fixed — SPA favicon 404 + nginx-direct static serving

`/stats/favicon.svg` was returning 404. The Vite build emits
`frontend/dist/favicon.svg` at the root of `dist/`, but FastAPI's
`/assets` mount only covered `dist/assets/*`, and the SPA catch-all at
`/{spa_path:path}` (web.py:2491) **deliberately** 404s requests ending
in known asset extensions to surface deploy bugs instead of returning
HTML for them. So the file existed on disk and nothing served it.

Two changes:

1. **FastAPI fallback** — added an explicit `GET /favicon.svg` route in
   `web.py` that returns the dist file via `FileResponse` with
   `Cache-Control: public, max-age=86400`. Works on first deploy with
   no nginx changes required.

2. **nginx-direct static serving** — `nginx-readsbstats.conf` now
   serves `/stats/assets/` and `/stats/favicon.svg` from
   `/opt/readsbstats/frontend/dist/` via `alias` instead of proxying to
   FastAPI. One fewer hop per static request, lighter on uvicorn. The
   FastAPI mounts stay registered as a fallback for direct `:8080`
   access (tests, dev), so the nginx alias is a pure perf
   optimisation — not load-bearing.

The nginx-direct path requires `www-data` to be able to read
`/opt/readsbstats/frontend/dist/`. `scripts/update.sh`'s recursive
`chown root:readsbstats; chmod u=rwX,g=rX,o=` locks "other" out, so
`update.sh` now also runs `usermod -aG readsbstats www-data`
(idempotent) and restarts nginx on the first add — group membership
only applies at process start, not on `systemctl reload`. After that
one-time restart, subsequent deploys are silent (the `if !
id -nG | grep -qx readsbstats` guard short-circuits).

### Added — DuckDB analytical accelerator for /api/map/heatmap and /api/map/coverage

`/api/map/heatmap?window=30d` and `?window=all` previously returned 504
from nginx (single-threaded SQLite GROUP BY over millions of `positions`
rows exceeded the 60 s `proxy_read_timeout`). Both endpoints now route
heavy aggregates through DuckDB's `sqlite_scanner` extension attached
read-only to the live SQLite file. Same on-disk DB, no migration, no
write path changes — DuckDB is a query-time accelerator only.
Vectorised multi-core scans drop the worst case from 60 s+ to ~5–15 s
on a Pi 4 with 3.3 M positions.

New module: `src/readsbstats/analytics.py`. Lazy singleton DuckDB
connection, double-checked init under a lock, per-call cursors so
concurrent endpoints don't serialise on the connection's internal
mutex, path validator for `ATTACH` / `SET temp_directory` (DuckDB has
no parameter binding for either, so paths become SQL text), three-layer
failure handling (import / first-connection / per-query), all with
fall-through to the original SQLite query so the endpoints can't
regress if the engine is unavailable.

Web-side additions:

- Per-window `asyncio.Lock` single-flight wrappers on both endpoints so
  two concurrent cold-cache misses don't both spawn a full-table scan.
- Background **prewarmer thread** (`map-prewarm`, daemon) that refreshes
  all 8 (heatmap × coverage × {24h, 7d, 30d, all}) cache entries at
  half-TTL. Users always hit warm cache; refreshes run one at a time
  with a 10 s cool-off between heavy queries so the warmup doesn't
  starve the collector. Gated on `RSBS_PREWARM_MAP_CACHE` (default on
  when DuckDB is on).
- Eager init in the FastAPI lifespan: the ~1–2 s extension-load + ATTACH
  cost is paid during service startup rather than the first user hit.

Configuration knobs (all opt-in, defaults safe):

| Var | Default | Purpose |
|---|---|---|
| `RSBS_USE_DUCKDB` | `0` (off) | Master flag; flip to `1` after deploy soak |
| `RSBS_DUCKDB_MEMORY_MB` | `256` | DuckDB working-set cap |
| `RSBS_DUCKDB_THREADS` | `2` | Worker threads (matches web's `CPUQuota=50%`) |
| `RSBS_DUCKDB_HOME_DIR` | `/mnt/ext/readsbstats/duckdb-home` | Extension cache + DuckDB state (the `readsbstats` system user has no `/home`) |
| `RSBS_DUCKDB_TEMP_DIR` | `/mnt/ext/readsbstats/duckdb-tmp` | Spill directory for queries exceeding the memory cap |
| `RSBS_PREWARM_MAP_CACHE` | `1` | Background prewarmer enable |

`scripts/update.sh` now pre-fetches the `sqlite_scanner` extension
binary at deploy time so the first user hit after a service restart
doesn't pay the ~5 s HTTPS download to `extensions.duckdb.org`. The
binary is cached in `$RSBS_DUCKDB_HOME_DIR/.duckdb/extensions/`.

Dependency: `duckdb==1.5.2` added to `requirements.txt` and
`pyproject.toml` (pinned, required, ~25 MB aarch64 wheel).

Tests: 9 new (`tests/test_analytics.py`: 8 parity + behaviour tests
including `cutoff_ts=None` / boundary-safe coords / fallback when
analytics is unavailable / per-query exception doesn't poison the
engine / env-flag flip without restart / path-validator rejects
injection; `tests/test_web.py::TestMapPrewarmer::test_prewarm_one_populates_cache`).
1197 Python passing, 0 regressions.

Two SQLite ↔ DuckDB math divergences caught during testing and
documented inline in `analytics.py`:

1. `CAST(double AS INTEGER)` rounds (banker's) in DuckDB; truncates in
   SQLite. The coverage SQL uses `FLOOR()::INTEGER` explicitly so
   bucket assignment matches.
2. `round(x, n)` uses banker's rounding in DuckDB; SQLite is half-up.
   Tests use boundary-safe coordinates to avoid the ≤0.01 % of
   cell-boundary points where the two engines disagree.

### Changed — CI workflow swapped vanilla-JS tests for frontend build + Vitest

`.github/workflows/test.yml` previously ran `node --check static/js/*.js`
and `node --test tests/js/test_*.mjs` — both directories were deleted at
v2.0.0 cutover, so every CI run on `main` failed. Replaced with
`corepack enable && corepack prepare npm@11 --activate` +
`npm ci --no-audit --no-fund` + `npm run build` (tsc -b + vite build) +
`npm test` (Vitest, 43 tests), all inside `frontend/`. Gated to the
Python 3.12 matrix slot. corepack used to swap Node 22's broken bundled
npm 10.9.4 (`Cannot find module 'promise-retry'` on `-g install`).

### Added — `frontend/.npmrc` registry pin

Pins `registry=https://registry.npmjs.org/` so lockfile regenerations on
the maintainer's dev machine (where `~/.npmrc` points at a company
artifactory) produce clean output that GitHub runners can actually fetch.
Without this pin, `npm ci` on CI hangs ~72 s and exits with the
misleading `Exit handler never called!` error.

### Fixed — `frontend/package-lock.json` regenerated, history rewritten

The lockfile that originally shipped with v2.0.0-rc.1 / v2.0.0 contained
507 `resolved` URLs pointing to an internal company artifactory (a
side-effect of the maintainer's `~/.npmrc` default). Lockfile regenerated
against the public registry; git history rewritten with `git filter-repo`
to strip the same URLs from every historical blob in two prior commits;
force-pushed to `origin/main`. `v2.0.0` and `v2.0.0-rc.1` tags also
force-pushed to the rewritten SHAs; stale `feat/react-ui` branch deleted
from remote. No code or feature change.

A local `.git/hooks/pre-commit` (not tracked) now greps staged diffs for
the offending hostnames and rejects the commit, as a backstop.

## 2.0.0 — 2026-05-16

### Removed — Jinja2 UI

The Jinja2 templates, vanilla JS, vendored uPlot/Leaflet assets, the v1
Playwright smoke file, and the `RSBS_ENABLE_V2` env-var kill-switch are
all deleted. The React SPA from v2.0.0-rc.1 is now the only UI, mounted
directly at `/stats/`. `static/airspace/` is the only surviving subdir
of `static/`.

### Changed — SPA mount moved from `/stats/v2/` to `/stats/`

Vite `base: '/stats/'`; React Router `basename: '/stats'`. The
`/stats/v2/*` URL space remains as a **301 redirect** to `/stats/*` so
v2.0.0-rc.1-era bookmarks keep working. `/live` is still a 302 to
`/map` (historical alias). The SPA catch-all is registered at the END
of `web.py` so it never shadows the `/api/*` routes.

### Added — nginx asset-cache + auto-reload in update.sh

`nginx-readsbstats.conf` gained a `/stats/assets/` nested location that
adds `expires 1y` + `Cache-Control: public, immutable` for the hashed
asset URLs. `index.html` continues to serve with `Cache-Control:
no-store` (every deploy rewrites the asset hashes inside it).
`scripts/update.sh` now runs `nginx -t && systemctl reload nginx` after
systemd daemon-reload, so deploys pick up nginx changes automatically.

### Tests

- **1190 Python + 43 Vitest + 81 Playwright = 1314** passing.
- The 69 vanilla-JS tests (`tests/js/`) and 35 v1 Playwright tests are
  deleted alongside the v1 surface they covered.

## 2.0.0-rc.1 — 2026-05-16

### Changed — web service memory cap raised (384M → 1024M)

`systemd/readsbstats-web.service` `MemoryMax` bumped from 384M to 1024M.
Driven by the v2 SPA's discoverable Heatmap toggle: `/api/map/heatmap` runs a
`GROUP BY round(lat, p), round(lon, p)` over the positions table, and the
SQLite in-memory sort/hash can spike to several hundred MB on a busy
receiver. The previous 384M cap got the worker OOM-killed mid-request.
Pi 4 has 8GB and steady-state utilisation around 1.7GB across all services,
so 1GB for the web worker is well within budget. Coverage and snapshot
endpoints unaffected. After the first hit per window the heatmap result is
cached (5 min for 24h, 30 min for 7d, 2h for 30d, 6h for all), so
subsequent toggles cost nothing.

### Added — v2 React SPA (coexists with the Jinja2 UI)

A complete React + Vite + TypeScript single-page app rebuild of the v1
Jinja2 UI ships alongside the original. Mounted at `/stats/v2/` whenever
`RSBS_ENABLE_V2=1` (default) AND `frontend/dist/` is built; otherwise the
mount silently doesn't register and the Jinja2 UI at `/stats/` is unaffected.
The Jinja2 UI is unchanged — no routes deleted, no behaviour modified.

**Stack:** React 19 + Compiler 1.0, Vite 7 (Rolldown), Tailwind CSS v4,
Radix UI primitives (Select, Dialog, Sheet, Popover, ToggleGroup, DropdownMenu,
Tooltip), TanStack Query v5, Zustand, React Router v7, Recharts,
react-leaflet 5 (Leaflet 1.9), Sonner toasts. React Compiler 1.0 enabled via
Babel plugin. Bundle: shell ~80 KB gz, vendor 34 KB, radix 30 KB, charts 112
KB lazy, leaflet 45 KB lazy; per-route chunks ≤ 8 KB.

**Pages shipped** (all at `/stats/v2/*` — same URL shape as v1):

- `/v2/` Statistics — summary cards, 24h/7d trend cards with delta arrows,
  flagged-flight counts, hourly + daily bar charts, DOW × hour heatmap, polar
  range plot, top types/airlines/countries/routes/airports, frequent + new
  aircraft, emergency squawks (clickable → history filter), personal records.
  Range picker with 24h/7d/30d/90d/All presets + Custom popover with
  datetime-local hour-precision pickers.
- `/v2/history` — filters (date range from/to, ICAO, callsign, registration,
  type, source, flag, squawk), sort headers, pagination, CSV export, URL
  state preservation, mobile-collapsing columns.
- `/v2/flight/{id}` — info card with photo + flag + source + squawk + airline,
  Leaflet route map with ADS-B / MLAT segment colouring + dark tile filter,
  Recharts altitude+speed ComposedChart, sampled positions log with RSSI
  colour bands, "Other flights by this aircraft" linked list.
- `/v2/aircraft/{icao}` — info card with photo, flag, country, first/last/
  duration, ✓ Watching / + Watch toggle (POST/DELETE `/api/watchlist`),
  full per-aircraft flights table.
- `/v2/gallery` — 1-row header (filter pills + sort icon Popover, both
  aligned right), card grid (60 per page), lazy photo loading, type-photo
  badge.
- `/v2/watchlist` — Add form, entries table, Radix Dialog delete confirmation,
  Sonner toast feedback, length-cap validation matching `database.WATCHLIST_*_MAX`.
- `/v2/feeders` — status table with manual refresh, "all-unavailable" notice,
  "not configured" empty state.
- `/v2/metrics` — 11 Recharts AreaChart panels (signal, aircraft, messages,
  range, positions, CPU, network out, network in, tracks, decoder, CPR),
  range picker matching `/v2/`, clickable health banner with per-check rows
  using a coloured left border for status (green/yellow/red/grey).
- `/v2/settings` — read-only display of all 39 runtime settings, secrets
  masked server-side via `_settings_payload()`.
- `/v2/map` — full-screen react-leaflet, Live + Rewind toggle, 10 s
  refetchInterval in Live mode, rewind slider with themed thumb,
  per-aircraft Sheet detail panel, **heatmap layer** (`leaflet.heat` on
  `/api/map/heatmap`) + 24h/7d/30d/all window selector, **coverage range
  overlay** (Leaflet `<Polygon>` on `/api/map/coverage`), **playback
  controls** (play/pause + ±10 m / ±1 h jump + 1×/2×/5×/10× speed
  buttons), **aircraft sidebar list** (left-side Sheet with 8-column
  sortable table — coexists with the right-side per-aircraft detail
  Sheet).

**Shared infrastructure:** Nav with brand, 8 links, units selector (Radix
Select), live aircraft count badge polling `/api/live` every 15 s (green dot
when active, click → `/map`), mobile hamburger.

**Backend additions (additive only, no v1 changes):**

- `GET /api/settings` — JSON mirror of `/settings` (single
  `_settings_payload()` source of truth).
- `GET /api/feeders` — JSON mirror of `/feeders`.
- `GET /api/flights` accepts `date_from` / `date_to` (YYYY-MM-DD, end-exclusive
  by adding 86400 to `date_to`) alongside the original single-`date` param.
  `date=` wins if both are set so v1 bookmarks keep working. Same params
  added to `/api/flights/export.csv`. Four pinning tests in
  `TestApiFlightsDateRange`.
- `RSBS_ENABLE_V2` env var (default `1`) — set to `0` for instant rollback;
  `web.py` SPA mount becomes a no-op and `/stats/v2/*` returns 404.

**Build & deploy:** `scripts/update.sh` aborts if `frontend/dist/index.html`
is older than anything under `frontend/src/` or if `package-lock.json` is
newer (unbuilt). Atomic-swap rsync: ship to `dist.new/`, mv server-side.
SPA `index.html` is served with `Cache-Control: no-store` (hashed asset URLs
inside change every deploy); assets get `public, immutable` via the optional
nginx asset-cache block.

**Tests:** 4 new Python tests (date range), 43 Vitest frontend unit tests
(format helpers, units store, CSRF wrapper, safe URL allowlist), 81 v2
Playwright tests across 6 device profiles covering every page (filter +
sort actually fire API calls, CSRF rejected without header, popover apply
writes URL params, live badge mounts, watch toggle round-trips, emergency
squawk links resolve, map rewind reveals slider, heatmap + coverage
toggles fire their API endpoints, sidebar list opens, playback advances
the slider, etc.). All v1 tests unchanged (1198 Python + 69 vanilla JS +
35 Playwright still pass).

**Plumbing notes for future cutover:**

- `tests/ui/_v2_app.py` — ASGI wrapper that strips `/stats/` from incoming
  paths for the v2 Playwright fixture (no nginx in tests).
- `useSearchParamBatch()` — multi-param URL updates must go through this,
  because React Router v7's `setSearchParams` reads stale `prev` when
  called twice in a row from the same handler. Single setters in onChange
  handlers stay fine.
- Telegram URLs stay pointed at `/stats/` (Jinja) throughout coexistence;
  flip at cutover commit 3.

**All 10 v2 pages reached full v1 parity** on 2026-05-16. No remaining
feature gaps before cutover. The four `/map` features (heatmap, coverage,
playback, sidebar) landed in the same session that hit parity.

**Post-cutover follow-ups** (tracked in
`internal_docs/uiux/v2-implementation-status.md` and
`internal_docs/internal/duckdb-analytics-plan.md`):
- DuckDB sqlite_scanner for analytical endpoints —
  `/api/map/heatmap?window=30d` currently times out at nginx (60 s) on
  busy receivers; DuckDB's vectorised multi-core GROUP BY over the same
  SQLite file should drop that to a few seconds. Deferred to post-cutover.
- v2.1 — `/metrics` to ECharts canvas + LTTB.
- v2.2 — stats heatmap + polar to ECharts native.
- v2.3 — `/map` to MapLibre GL v5.
- Mobile filter pane on `/v2/history`; scroll-fade mask on overflowing
  tables.

### Changed (breaking — env var rename, no back-compat shim)

- **`RSBS_BASE_URL` renamed to `RSBS_TELEGRAM_BASE_URL`** — and the
  corresponding `config.BASE_URL` attribute renamed to
  `config.TELEGRAM_BASE_URL`. The variable is only used to build profile /
  flight links in Telegram alerts, so the new name matches the rest of
  the Telegram-scoped namespace (`RSBS_TELEGRAM_TOKEN`,
  `RSBS_TELEGRAM_CHAT_ID`, `RSBS_TELEGRAM_UNITS`, `RSBS_TELEGRAM_PHOTOS`,
  `RSBS_TELEGRAM_ANONYMOUS_ALERT`). Deployments must update their systemd
  environment file at upgrade time — there is no fallback to the old name.
  README env-var table and the example systemd snippet both updated; the
  `/settings` page now displays the new env var name. No version bump
  required (release this with the next non-trivial change).

## 1.8.2 — 2026-05-13

Closes a small daily-summary coverage gap noted while back-filling the
FLAG_ANONYMOUS README documentation in v1.8.1: `send_daily_summary`
emitted Military and Interesting counts but never carried over the
**Anonymous** count, despite the rest of the codebase (badges, web UI,
`/api/stats`, first-sighting Telegram alerts) all treating anonymous
as a peer flag.

### Added

- **Anonymous count in the daily Telegram summary** — the summary's
  aggregate query now OR-merges `aircraft_db.flags` + `adsbx_overrides.flags`
  with the computed anonymous bit (via `icao_ranges.anonymous_flag_sql`),
  applies the same military > interesting > anonymous precedence used
  everywhere else (each flight counts under exactly one kind), and emits
  a new "Anonymous: N" badge alongside "Military: N" / "Interesting: N"
  when N > 0. Zero-suppression matches the existing badges. Four new
  tests in `TestSendDailySummary`: bare anonymous-badge presence, both
  precedence-exclusion directions (military and interesting suppress
  the anon count), and zero-suppression when no anon flights exist.
  Resolves a documentation-vs-behaviour drift surfaced during the v1.8.1
  README anonymous-aircraft pass.

### Changed

- README's daily-summary description (Telegram section) now lists
  `military/interesting/anonymous counts` with an explicit note about
  the shared precedence rule.

### Test counts

- Python: **1188 passing** (was 1184) — +4 from this release.
- JS: 69 passing (unchanged).
- Playwright UI: 35 (unchanged).

## 1.8.1 — 2026-05-13

Follow-up from the eleventh audit pass — bug fixes, a defensive refactor on
the Telegram path, test coverage for the in-process enrichment caches, and
five small cleanups. No new features; deploy is in-place.

### Fixed

- **`/api/metrics` returned HTTP 500 on non-integer `from` / `to`** — the
  handler called `int(request.query_params.get("from", ...))` directly, so
  garbage input bubbled up as `ValueError` and FastAPI mapped it to 500
  instead of 400. Switched to typed `Query(None, alias="from")` parameters
  so the validation layer rejects them at the boundary with 422. Five new
  tests in `tests/test_web.py::TestApiMetricsQueryValidation`.
- **Telegram `/unwatch <hex>` could delete a registration-typed watchlist
  entry with the same literal value** — `_watch_remove` ran `DELETE FROM
  watchlist WHERE value = ?` with no `match_type` filter. Now mirrors the
  `_watch_add` inference (`re.fullmatch(r"[0-9a-f]{6}", value)` → `icao`,
  else `registration`) and adds `AND match_type = ?` to the DELETE. The
  HTTP `DELETE /api/watchlist/{id}` endpoint remains the authoritative
  cross-type removal path. Three new tests pin the behaviour.
- **`scripts/notify-telegram-failure.sh` silently dropped alerts when the
  failing unit's journal contained `<`, `>`, or `&`** — Telegram's
  `parse_mode=HTML` returns 400 on those characters even inside text
  nodes, so a single weird-looking traceback would suppress the very
  alert you most needed. The shell script now pipes `systemctl status`
  output through `sed` to HTML-escape `&`/`<`/`>` (in that order — `&`
  first so it doesn't eat the entities the later substitutions emit)
  before interpolation into the `<pre>...</pre>` block.

### Changed

- **All Telegram outbound calls now route through `http_safe.safe_urlopen`**
  — `notifier._send`, `_send_photo`, and `_get_updates` previously used raw
  `urllib.request.urlopen`, bypassing the central SSRF guard's redirect
  blocker and response-size cap. To make this work for the POSTs,
  `safe_urlopen` gained an optional `data: bytes | None = None` parameter
  that flows into `urllib.request.Request`. All four policies (HTTPS-only,
  public-IP-only, no-redirect, size cap) now apply uniformly to every
  outbound call in the codebase, including `api.telegram.org`. If Telegram
  ever 302s during a region migration, the notifier will surface the
  redirect as a `ValueError` instead of following blindly. Four new tests
  in `test_http_safe.py` cover the POST capability; the existing 13 mock
  sites in `test_notifier.py` were rewritten to patch the new symbol.
- **`templates/base.html` active-nav match now requires a segment boundary**
  — `path.startsWith(href)` would have lit up `/history` on a future
  `/history-archive` path. Tightened to `path === href || (href !== "/"
  && path.startsWith(href + "/"))`. Cosmetic only — none of the current
  nav routes have this collision.
- **`<select>` option value `aero` renamed to `aeronautical`** — matches
  the existing `config.TELEGRAM_UNITS == "aeronautical"` literal in the
  Python notifier. `initUnitSelector()` carries a one-time migration that
  rewrites a stored `"aero"` in `localStorage` to `"aeronautical"` so
  existing users see the correct option highlighted in the dropdown after
  the rename. Frontend behaviour is unchanged in every other respect.
- **`_csrf_check` gained a load-bearing `# CRITICAL` comment** explaining
  that the X-Requested-With header check works *because* this app has no
  CORS middleware that whitelists custom headers. A future engineer
  adding `CORSMiddleware(allow_headers=["*"])` will now hit the warning
  at the diff level.

### Removed

- **Deleted dead code: `static/js/live.js` (116 lines) and
  `templates/live.html` (34 lines)** — the `/live` route has been a
  301-redirect to `/map` since v1.4.0 and never actually rendered the
  template. Pre-flight grep confirmed nothing else referenced them. The
  `/api/live` JSON endpoint (used by the nav live-badge) is independent
  and stays. Tests that probe the `/live` redirect (`test_web.py:750`,
  `test_map.py:228`, `tests/ui/test_mobile_smoke.py:148`) still pass.

### Tests

- **New `tests/test_enrichment.py` (20 tests)** — direct coverage for
  `_LRUDict` (basic put/get, None-as-value vs. miss, eviction at maxsize,
  LRU touch-to-end) and its thread safety (8-thread concurrent put + get
  with 1000 ops each + a concurrent-clear test). Plus negative-cache and
  positive-cache behaviour of `lookup_aircraft`, `lookup_airline`, and
  `lookup_adsbx`, the `invalidate_adsbx` busting path, and `clear_cache`
  resetting all three module-level caches. Closes the longest-standing
  test-coverage hole (the enrichment module was previously covered only
  transitively via collector / web tests).

### Internal cleanups

- Dropped unused `import math` / `import sys` from `scripts/purge_ghosts.py`,
  `scripts/purge_bad_gs.py`, and `scripts/purge_mlat_gs_spikes.py`.
- Tightened the "Background migrations — single owner" paragraph in
  `CLAUDE.md` to spell out which indexes belong in `_migrate()` (small
  `flights` table, e.g. `idx_flights_max_gs` / `idx_flights_max_alt`) vs
  in `run_background_migrations()` (heavy composite/partial indexes on
  the millions-row `positions` table).

### Test counts

- Python: **1184 passing** (was 1152) — +32 from this release.
- JS: 69 passing (unchanged).
- Playwright UI: 35 (unchanged).

## 1.8.0 — 2026-05-13

### Added

- **`FLAG_ANONYMOUS` — surface non-ICAO Mode-S addresses** — a new flag bit
  (16) is computed at query time from `src/readsbstats/icao_ranges.py` and
  OR-merged into every flag projection alongside `aircraft_db.flags` and
  `adsbx_overrides.flags`. An address gets the bit set when it falls outside
  every ICAO state-allocated block — typically military / OPSEC contacts,
  TIS-B / ADS-R rebroadcasts, and MLAT-synthetic identifiers. No DB column,
  no backfill: editing the state-allocation table retroactively reclassifies
  every historical flight on the next query. Motivated by a real-world
  sighting of hex `dd85cb` (a clean westbound transit across central Poland
  whose Mode-S address ADSBExchange flagged as "Non-ICAO hex (dynamic)").
  - **Filters and gallery** — `flags=anonymous` on `/api/flights` and
    `/api/aircraft/flagged` returns anon-only contacts (military / interesting
    take precedence under their own filters, matching the existing
    interesting/military exclusion pattern). The "All" tab on the flagged
    gallery now includes anonymous hits alongside military and interesting.
    New "Anonymous" filter button in `templates/gallery.html`.
  - **UI badge** — `flagBadge()` in `static/js/table-utils.js` gains a third
    branch with a `"?"` short label / `"Anonymous"` long label and a new
    `.badge-anon` CSS class. Precedence stays military > interesting >
    anonymous so the existing badges aren't disturbed.
  - **Stats page** — new "Anonymous" mini-stat alongside "Military" and
    "Interesting" inside the redesigned flagged-flights card.
  - **Telegram alert** — `notifier.notify_anonymous()` fires once per
    first-ever-sighting of a non-ICAO hex (Country line intentionally
    omitted since the address has no state by definition). Gated by
    `RSBS_TELEGRAM_ANONYMOUS_ALERT` (default `1`). `_load_notified()` is
    extended via a `LEFT JOIN` + the anon CASE so a restart doesn't re-fire
    historical anon alerts.
  - **Retention** — `_close_flight()` ghost-purge exemption is extended to
    keep single-position anonymous sightings (same precedent as military /
    interesting). The whole point of the flag is to surface edge-of-range
    contacts, so a one-sample track is exactly what we want to preserve.

### Changed

- **Stats page top-card redesign** — added a third mini-stat (Anonymous)
  next to Military and Interesting. To keep all summary cards at the same
  height, the flagged card now lays its three sub-stats out horizontally
  inside one card that spans two grid cells, with thin vertical separators
  between sub-cells. The standalone "Furthest detected" card is removed
  from the top strip (it remains in the Records section below).
- **`stats.js` adopts the shared `flagBadge()`** — two ad-hoc inline badge
  renderers in the "New aircraft" and "Frequent aircraft" sections were
  replaced with a single `flagBadge(flags, "short")` call so the new
  Anonymous badge propagates without three more copy-paste edits.

### Fixed

- **`icao_ranges._RAW` was missing Qatar (0x06A000–0x06A3FF) and South
  Sudan (0x06A400–0x06A7FF)** — the `FLAG_ANONYMOUS` audit on the live
  35 k-flight DB flagged 60+ Qatar Airways (A7-Bxx) aircraft as anonymous,
  which is a table-gap bug rather than a real anon contact. Added both
  allocations; the anon-flight count on the same DB dropped 181 → 46
  (97 unique → 33 unique aircraft) after the fix. Added a regression
  test pinning the new ranges.

### Operations

- **Perf, measured on the live 35 k-flight DB:** `/api/flights` page-1 with
  `_FLIGHT_COLS` = 22 ms; full-table stats scan with the anon CASE
  = 379 ms (cached 120 s); `_load_notified` startup scan = 133 ms. The
  CASE expression embeds ~10 KB of state-range conditions into every
  flag-projecting query, but SQLite's prepared-statement cache amortises
  parsing across calls because every call site uses the same string literal.
- **Audit the table when this feature surfaces a clean operator.** If
  the gallery shows a familiar callsign (e.g. `OMS681` SalamAir,
  `T7-WHK` San Marino) as anonymous, look up the hex on
  `https://hexdb.io/api/v1/aircraft/<hex>` first to decide whether it's a
  real anonymous contact or a missing state allocation worth adding to
  `_RAW`. The Qatar miss was the canonical example.

### Tests

- +44 Python tests and +7 JS tests. New Python classes / cases:
  `TestIsAnonymousIcao` (10), `TestAnonymousFlagSql` (4 — SQL/Python
  parity), `TestQatar*` (2 regression pins), `TestAnonymousFlagInResponse`
  (4 end-to-end via `/api/flights`),
  `TestApiFlaggedAircraft::test_filter_anonymous_only` +
  `test_all_filter_includes_anonymous`,
  `TestApiStats::test_anonymous_flights_counted_separately` and
  `test_stats_shape` extended,
  `test_close_flight_keeps_anonymous_hex_with_few_positions` +
  `test_enrich_sets_anonymous_flag_for_non_state_hex`,
  `TestLoadNotified::test_loads_anonymous_icao_without_aircraft_db_row` +
  `test_does_not_load_state_allocated_icao_without_flags`,
  `TestDispatchOne` (2 routing cases),
  `TestNotifyAnonymous` (5). JS: 7 `flagBadge` precedence cases in
  `tests/js/test_table_utils.mjs`. Total suite:
  **1152 Python + 69 JS + 35 Playwright UI**, all passing.

## 1.7.1 — 2026-05-12

### Fixed

- **nginx CSP blocks Wikipedia type photos** — `upload.wikimedia.org` was
  missing from the `img-src` directive in `nginx-readsbstats.conf`, causing
  browsers to block the new Wikipedia fallback images with a Content Security
  Policy violation. Added `https://upload.wikimedia.org` to `img-src`.
  Apply by reloading nginx: `sudo nginx -t && sudo systemctl reload nginx`.

### Tests

- Fix race condition in `test_all_three_emergency_squawks_trigger` — the test
  asserted `squawk_calls` immediately after three `_poll()` calls without
  waiting for the async consumer thread to drain the queue, so the last
  notification (7700) was consistently missing on CI. Added
  `_drain_notifications(timeout=1.0)` before the assertion, matching the
  pattern used in every other notification test. Hardened
  `test_emergency_squawk_not_repeated_same_flight` with the same drain call.

## 1.7.0 — 2026-05-11

### Added

- **Wikipedia type-photo fallback** — the photo lookup ladder gains a sixth
  step that queries Wikipedia for a representative photo when the existing
  chain (Planespotters → airport-data.com → hexdb.io) misses for both the
  specific aircraft and a probe ICAO of the same type. Resolution is a
  two-hop call: Wikipedia's `opensearch` endpoint maps `aircraft_db.type_desc`
  (e.g. `"BOEING 737-800"`) to a canonical article title, then
  `/api/rest_v1/page/summary/{title}` returns `thumbnail` + `originalimage` +
  article URL. Result is stored in `type_photos` with
  `photographer="Wikipedia"` and `link_url` pointing to the article
  (CC-BY-SA attribution). Disambiguation pages, missing thumbnails,
  400 / 404 / 410, and malformed responses all return a clean miss. Closes
  the gap for vintage, military, GA, and rotorcraft types that the commercial
  photo APIs under-cover (e.g. `MIG29`, `C152`, `EUFI`, `H60`, `AN26`,
  `BE20`).
  - **Defence-in-depth URL allowlist** — returned photo URLs are constrained
    to `upload.wikimedia.org` (HTTPS) and the article link to
    `en.wikipedia.org`. A wiki edit pointing the infobox image at an
    attacker-controlled host gets dropped before it lands in the cache.
  - **Telegram alerts benefit automatically** — `notifier._get_photo_result`
    already routes through the shared `resolve_photo` ladder, and Wikipedia
    URLs on `upload.wikimedia.org` pass the existing SSRF guard and the
    10 MB download cap.
  - **New env var** `RSBS_WIKIPEDIA_PHOTO` (default `1`) — set to `0` to
    skip step 6 entirely. Lives next to `PHOTO_CACHE_DAYS` and
    `TELEGRAM_PHOTOS` in `config.py`. Toggling it does **not** invalidate
    already-written `type_photos` rows; use
    `DELETE FROM type_photos WHERE photographer='Wikipedia'` (or
    `DELETE FROM type_photos WHERE thumbnail_url IS NULL` for negative rows)
    to force re-evaluation.

### Changed

- **`web._fetch_type_photo` now delegates to `photo_sources.resolve_photo`** —
  removed ~90 lines of duplicated ladder logic between the web and notifier
  paths. Both code paths share a single source of truth for the
  cache → JOIN → probe → Wikipedia sequence. The async wrapper keeps the
  per-type `asyncio.Lock` and a cache-hit fast path so the hot read avoids
  the executor hop entirely. `resolve_photo` now supports a "type-only" mode
  when called with `icao_hex=""` — steps 1 and 4 (the icao-keyed cache and
  fetch) are skipped so the type-only caller doesn't pollute the `photos`
  table with an empty-key row.

### Fixed

- **Photo credit attribution on the frontend** — the `loadPhoto()` credit
  line in `static/js/table-utils.js` previously hardcoded
  `"© {photographer} via Planespotters.net"` for every hit, which was
  already wrong for airport-data and hexdb hits and would have rendered
  `"© Wikipedia via Planespotters.net"` for the new fallback. Replaced
  with a new `photoSourceSuffix(link)` helper that derives the source
  label from the link URL's hostname (Planespotters.net /
  airport-data.com / hexdb.io / Wikipedia); empty suffix when the link is
  missing or on an unrecognised host.

### Operations

- New log lines at `DEBUG` from the `photo_sources` logger on every
  Wikipedia step-6 outcome (hit / miss / failure) — same convention as
  the rest of the photo chain. For ongoing visibility, query the cache
  directly:

  ```sh
  sqlite3 /mnt/ext/readsbstats/history.db \
    "SELECT type_code, link_url,
            datetime(fetched_at,'unixepoch','localtime') AS fetched
     FROM type_photos WHERE photographer='Wikipedia'
     ORDER BY fetched_at DESC;"
  ```

### Tests

- +23 Python tests and 8 JS tests. `TestFetchWikipediaType` (14 cases)
  covers defensive parsing, host allowlist, percent-encoding, HTTP
  400 / 404 / 410 / 429 / 500 handling, User-Agent header, and missing /
  non-list / non-string field handling. `TestUrlHostMatches` (4) covers
  the host-allowlist helper. `TestResolvePhoto::test_wikipedia_*` (7)
  covers integration with the ladder, including the new type-only mode.
  `TestFetchTypePhoto` and `TestGetPhotoResult` each gain a Wikipedia
  end-to-end test. Existing test classes get an autouse fixture that
  disables `_WIKIPEDIA_ENABLED` so probe-miss tests don't accidentally
  hit the network. Total suite: 1108 Python + 62 JS, all passing.

## 1.6.0 — 2026-05-11

### Security

- **Centralised SSRF guard** — new `src/readsbstats/http_safe.py` module
  enforces HTTPS-only, public-IP-only (rejects RFC1918 / loopback / link-local /
  metadata / reserved / multicast addresses via `ipaddress` checks), no
  auto-redirect, and a per-call response-size cap on every outbound HTTP
  request. Two entry points: `safe_urlopen()` for `urllib` callers and
  `safe_httpx_get()` for `httpx` callers. Adopted by `photo_sources`
  (256 KB / 10 MB caps), `route_enricher` (64 KB; callsign now percent-encoded),
  `adsbx_enricher` (4 MB), and `db_updater` (50 MB).
  `db_updater.AIRCRAFT_CSV_URL` switched to the direct `raw.githubusercontent.com`
  URL so the redirect-blocking policy doesn't break the import.
- **HTML-escape Telegram captions** — `registration`, `callsign`, watchlist
  `label`, `type_desc`, `country`, `squawk`, and the daily-summary DB-JOIN
  columns are all run through `notifier._h()` before HTML interpolation.
  Prior to this, a `&` / `<` / `>` in any of those fields caused Telegram's
  `parse_mode=HTML` to return 400 and the alert was silently dropped.
- **Structure-aware caption clamp** — `_clamp_caption` replaces
  `_truncate_caption`. Over-limit captions first drop the trailing
  `<i>Photo …</i>` note, then the trailing `<a href="…">…</a>` link line,
  then plain-truncate the body with `…`. Prevents the previous cut-in-the-middle
  of an `href=` attribute that would also produce a 400 from Telegram.

### Operations

- **Collector failure alert** — `notify-telegram@.service` fires via `OnFailure=`
  when the collector permanently fails (after exhausting `StartLimitBurst=5`
  restarts). Sends a Telegram message with the last 30 lines of `systemctl status`
  output. Reads `RSBS_TELEGRAM_TOKEN` / `RSBS_TELEGRAM_CHAT_ID` from the existing
  env file — no extra configuration required.

### Features

- **Shared photo-source module** — `photo_sources.py` centralises the
  Planespotters → airport-data.com → hexdb.io fallback chain. Both the web
  service (`web.py` via `run_in_executor`) and the notifier use the same chain.
  `SOURCES` is the single extension point: append a new callable to add a source.
- **Shared photo lookup ladder** — `photo_sources.resolve_photo()` factors the
  5-step cache → JOIN → fetch → probe ladder used by
  `notifier._get_photo_result`. The web side keeps its own `_fetch_photo` /
  `_fetch_type_photo` split for the asyncio path.

### Bug fixes

- **Telegram photo delivery** — Planespotters blocks hotlink requests from
  Telegram's bot servers. Photos are now downloaded locally (up to 10 MB)
  and uploaded to the `sendPhoto` API as `multipart/form-data`, so the image
  always arrives in the chat. Content-Type detection maps JPEG / PNG / WebP to
  the correct filename. Drops the dead URL-payload fallback (it almost always
  failed for the same reason). Multipart boundary is randomized per upload via
  `secrets.token_hex(16)`.

- **Photo fallback in Telegram alerts** — when no specific aircraft photo exists,
  the notifier now tries airport-data.com and hexdb.io before giving up (previously
  only Planespotters was checked).

### Performance

- **Notification dispatch queue** — `_poll()` no longer spawns a daemon thread
  per call. A single long-lived consumer thread (`tg-dispatch`, started in
  `collector.main()`) reads alerts off `collector._notification_queue` and
  dispatches them serially. The consumer holds one sqlite connection for its
  lifetime (via `notifier._thread_local`) instead of reopening per alert.
  Eliminates thread pileup under bursty alerts; ~5–10 ms saved per alert from
  connection reuse.

## 1.5.2 — 2026-05-09

### Tests

- Deflake `test_index_build_under_concurrent_writes` — on the small CI
  runner the writer thread could be scheduled out for the entire microsecond
  index build and never get its first INSERT in, failing the
  `count > 0` assertion. Added a `threading.Event` barrier so the test
  blocks on the first successful write before the index build begins.
  No production-code change.

## 1.5.1 — 2026-05-09

Production-readiness sweep (seventh audit pass — see
`internal_docs/improvements.md` items #86–101).

### Performance & reliability

- **Per-thread sqlite connections in web** — `web.py::db()` now lazily opens a
  connection per uvicorn worker thread via `threading.local()`. Python's
  per-connection sqlite mutex previously serialised every request through one
  global lock, throwing away WAL's reader concurrency.
- **30 read-only `async def` handlers → `def`** — FastAPI now dispatches them
  to its threadpool, freeing the asyncio event loop. Endpoints that genuinely
  `await` (photo fetchers, heatmap/coverage, feeder checks) stay async.
- **Watchdog heartbeat is now its own thread** — `_watchdog_loop()` ticks every
  20 s independent of the poll loop. A write inside `_poll()` can block on the
  SQLite write lock for tens of seconds while a background `CREATE INDEX` is
  running; the previous inline `WATCHDOG=1` would have missed `WatchdogSec=60`
  and had systemd kill the collector.
- **Single-source background migrations** — `run_background_migrations()` now
  runs only in the collector. Web no longer spawns a duplicate thread that
  would race on the same `CREATE INDEX` and `backfill_bearing` UPDATEs.
- **`/api/dates` cached** (TTL 600 s) — was doing a full GROUP BY scan of
  `flights` on every request even though the result only ticks daily.
- **Partial index `idx_positions_ts_coords`** — `ON positions(ts) WHERE lat IS
  NOT NULL AND lon IS NOT NULL`. Speeds up cold-cache heatmap/coverage when
  many MLAT-only rows have NULL coords.
- **Background helpers guard `conn.close()`** — `_build_positions_indexes` and
  `backfill_bearing` no longer mask a real `connect()` failure with
  `UnboundLocalError` from the `finally` block.

### Security

- **Telegram bot token redacted in error logs** — `notifier._describe_exc()`
  formats `urllib.error.HTTPError` / `URLError` without echoing the request URL
  (which contains `/bot<TOKEN>/`). Defence in depth: current stdlib `__str__`
  doesn't leak the URL, but third-party libs and future stdlib changes might.
- **`safeHttpUrl()` tightened to `^https://`** — the third-party photo
  providers (Planespotters, airport-data, hexdb) all serve over HTTPS already;
  rejecting `http://` closes the MITM window for users on hostile networks.
  Does not affect the readsbstats app's own URLs (those are relative and never
  go through this function), so HTTP-only LAN deployments are unaffected.
- **Auto DB snapshot before purge `--apply`** — `database.snapshot_db()` does
  an atomic `VACUUM INTO <db>.backup-<ts>.db` before any of the three
  `purge_*.py` scripts mutate. `--i-have-a-backup` opts out.

### Tests

- 972 Python tests (was 948) + 54 JS tests. New: per-thread connection
  behaviour, watchdog loop lifecycle, Telegram token-redaction caplog
  assertions, partial-index DDL verification, snapshot helper, concurrent
  writer + index builder, `backfill_bearing` against out-of-range coords,
  notifier "Country: Unknown" fallback.

### Cleanup

- Removed duplicate `end = last_update` assignment in `import_rrd.py`
- uPlot chart instances destroyed on `beforeunload` in `metrics.js`
- `safeHttpUrl()` JS test suite updated to lock the new https-only contract

## 1.1.1 — 2026-04-26

### Security

- Block `javascript:` / `data:` URIs in third-party photo links via a
  `safeHttpUrl()` allowlist
- Require `X-Requested-With` header on watchlist `POST` / `DELETE` (CSRF
  defence — browsers cannot set custom headers cross-origin without a CORS
  preflight that this app rejects)
- Cap watchlist `value` (64 chars) and `label` (255 chars) lengths at the
  Pydantic-model layer; the same caps are enforced in the Telegram `/watch`
  bot command path
- Show only the database filename, not the full path, on `/settings`
- `safeHttpUrl()` now returns the trimmed URL for consistency

## 1.1.0 — 2026-04-24

- Receiver health dashboard with metrics time-series (43 columns) and 9
  health checks (heartbeat, aircraft visibility, message rate, signal drop,
  CPU saturation, gain hints, range degradation)

## 1.0.0 — 2026-04-17

Initial public release.

### Features

- Collector daemon polling readsb `aircraft.json` every 5 seconds
- Automatic flight grouping (30-minute silence gap)
- ADS-B vs MLAT source tracking per position and per flight
- SQLite database with WAL mode (no external DB server)
- Aircraft enrichment from tar1090-db (~620k aircraft)
- Airline names from OpenFlights
- Aircraft photos from Planespotters.net (cached 30 days)
- Route enrichment via adsbdb.com (origin/destination airports)
- ICAO address range country lookup
- Ghost position filter (real-time + historical cleanup)
- Telegram notifications (military, interesting, emergency squawks, daily summary, watchlist)
- Aircraft watchlist (ICAO hex, registration, callsign prefix)

### Web UI

- Statistics dashboard with date-range picker and trend deltas
- Flight history with search, filter, sort, and CSV export
- Per-flight detail with Leaflet map, altitude/speed profile, RSSI chart
- Aircraft detail page with full history and country of origin
- Live flight board with auto-refresh
- Airspace overlay (CTR/TMA/R/D/P zones via GeoJSON)
- Polar range plot
- All-time personal records
- Unit switching (aeronautical / metric / imperial)
- Watchlist and settings pages
