# Operations

## Updating code

After syncing new source files to the Pi, run as root:

```bash
# Sync code and restart services (most common)
bash /opt/readsbstats/scripts/update.sh

# Sync code only, also re-download aircraft/airline database (no service restart)
bash /opt/readsbstats/scripts/update.sh --db-only

# Sync code, restart services, and re-download database
bash /opt/readsbstats/scripts/update.sh --full
```

All three modes sync code first. Only `--db-only` and `--full` trigger a database download (~30 seconds, ~10 MB). The collector is stopped automatically during the download to avoid SQLite write conflicts.

## Aircraft & airline database

Registration, aircraft type, and airline name data is updated weekly by a systemd timer:

```bash
# Manual update
bash /opt/readsbstats/scripts/update.sh --db-only

# Check timer status
systemctl status readsbstats-updater.timer

# Check last run log
journalctl -u readsbstats-updater -n 30
```

The updater also backfills existing flights that have missing registration or type data.

## Useful commands

```bash
# Service status
systemctl status readsbstats-collector readsbstats-web

# Live collector log
journalctl -u readsbstats-collector -f

# Web server log
journalctl -u readsbstats-web -f

# Restart after config change
systemctl restart readsbstats-collector readsbstats-web

# Stop and disable everything
systemctl disable --now readsbstats-collector readsbstats-web readsbstats-updater.timer
```

## RRD history import

To backfill `receiver_stats` from existing graphs1090 RRD files:

```bash
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/import_rrd.py
```

Maps 20 RRD files to 21 receiver metric columns. Uses `INSERT OR IGNORE` to preserve finer-grained data across multiple resolution tiers.

## Receiver metrics

Enable metrics collection from `/run/readsb/stats.json`:

```ini
Environment="RSBS_METRICS_ENABLED=1"
```

The collector polls every 60 s and stores 43 metrics in `receiver_stats`. The `/metrics` page displays 10 time-series charts with automatic downsampling (raw ≤24h, 5-min ≤7d, 15-min ≤30d, 1-hour ≤90d).

## Polar range plot tuning

The plot auto-scales ring spacing to your actual max detection range. To adjust angular bucket size, edit `BUCKET_DEG` in `src/readsbstats/api/stats.py` → `api_stats_polar`:

| `BUCKET_DEG` | Buckets | Use when |
|---|---|---|
| `5` | 72 | Dense data, fine directional detail |
| `10` | 36 | Default |
| `15` | 24 | Sparse data or uneven coverage |

## Database backups

All purge scripts auto-snapshot to `<db>.backup-<ts>.db` before mutating. Skip with `--i-have-a-backup`.

```bash
# Manual snapshot
/opt/readsbstats/venv/bin/python -c "
from readsbstats.database import snapshot_db
snapshot_db('/mnt/ext/readsbstats/history.db')
"
```

## Deployment security

readsbstats ships **no authentication and no authorization by default**. The
trust model is "bind to loopback, sit behind nginx on a trusted LAN."

**What this means in practice:**

- The uvicorn app listens on `127.0.0.1:8080`. nginx proxies `/stats/` to it.
  Anyone who can reach that port (directly, or through nginx) can read every
  flight, aircraft, and metrics endpoint **and** call every mutating endpoint
  (watchlist add/remove, settings writes).
- **The `X-Requested-With: XMLHttpRequest` CSRF check is not authentication.**
  It only stops a third-party web page from silently POSTing to the API in a
  logged-in browser. It does not identify, authenticate, or authorize the
  caller — a direct `curl` with that header passes it. Never treat it as access
  control.
- **Optional bearer-token gate (audit 2026-05-31 SH-1).** Setting
  `RSBS_API_TOKEN=<value>` requires every mutating call to carry
  `Authorization: Bearer <value>`; comparison uses `hmac.compare_digest`. No-op
  when the env var is unset (default trusted-LAN posture unchanged). Read
  endpoints are NOT gated — they were already public on the trusted LAN. This
  is a thin extra layer for deployments where the LAN itself isn't fully
  trusted; it is **not** a substitute for a reverse-proxy auth layer when the
  app is reachable from the public internet.
- **`flight_id` path params are integer-typed; `{icao_hex}` path params are
  validated** against `^~?[0-9a-fA-F]{6}$` before any DB or outbound work, and
  feeder `status_path` values are `realpath`-checked under `RSBS_FEEDER_STATUS_ROOT`.
  These are defence-in-depth (bounding side effects), **not** a substitute for
  network-level access control.

**If you expose the UI beyond a trusted LAN**, put an authenticating layer in
front of nginx — HTTP basic auth, an OAuth2 reverse proxy (e.g. oauth2-proxy),
or a VPN / Tailscale tailnet. Do not publish `127.0.0.1:8080` or the nginx
`/stats/` location to the public internet without one. `RSBS_API_TOKEN` alone
is not enough for public exposure.

**Outbound HTTP** (photo + route enrichment) is centralised in `http_safe.py`
with an SSRF guard (HTTPS-only, globally-reachable-IP-only, multicast-blocked,
redirect-blocked). Provider photo URLs are additionally checked against
per-source CDN host allowlists before they are cached, and at the API response
boundary on every photo emission (audit 2026-05-31 PY-6) so cached
off-allowlist URLs never reach the SPA regardless of `RSBS_PHOTO_HOST_ENFORCE`.
The image-host and link-host allowlists are separate — see `docs/configuration.md`.

## Database integrity & startup recovery

The collector writes a `.dirty_shutdown` sentinel next to the database on
startup and removes it only on a clean shutdown. If the sentinel is present at
the next boot (i.e. the previous run crashed or was power-cut), the collector
runs `PRAGMA quick_check(10)` **before** it begins polling.

**Fail-closed behaviour:** if the check finds corruption — or if `quick_check`
cannot even run — the collector sends a Telegram alert, notifies systemd, and
exits with code `2` **without** starting the poll loop, so it never writes to a
possibly-corrupt database. The sentinel is left in place, so the check repeats
on every restart until an operator intervenes. `Restart=on-failure` with
`StartLimitBurst=5` / `StartLimitIntervalSec=120` bounds this to ~5 restarts
before systemd parks the unit in `failed` state (which fires
`OnFailure=notify-telegram@…`).

**Recovery steps** (run as root, collector stopped):

```bash
systemctl stop readsbstats-collector
DB=/mnt/ext/readsbstats/history.db

# 1. Snapshot the corrupt file first (forensics / second attempt).
cp "$DB" "$DB.corrupt-$(date +%s)"

# 2a. Preferred: restore the most recent good backup.
ls -t "$DB".backup-*.db
cp "$DB".backup-<ts>.db "$DB"

# 2b. Or salvage in place with the SQLite recovery tool.
sqlite3 "$DB" ".recover" | sqlite3 "$DB.recovered"
mv "$DB.recovered" "$DB"

# 3. Clear the sentinel so the integrity check passes on next start.
rm -f /mnt/ext/readsbstats/.dirty_shutdown

systemctl start readsbstats-collector
```

Verify with `journalctl -u readsbstats-collector -n 30` — a clean start logs
`DB integrity check passed; checkpointing WAL`.
