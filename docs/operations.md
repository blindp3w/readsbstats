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

## VDL2 / ACARS ingest

Opt-in feature (default off). Decodes VHF Data Link Mode 2 / ACARS from a
**separate SDR** and stores messages in their own `vdl2.db` — the core
`history.db` is never touched. readsbstats is **consume-only**: you run the
decoder; readsbstats runs only the UDP ingest listener and the web tab.

**Turning it on** — one flag, one enable:

1. Set `RSBS_VDL2_ENABLED=true` in `/etc/readsbstats/vdl2.env` (the single shared
   file, read by both `readsbstats-web` and `readsbstats-vdl2`). `install.sh`
   seeds it from the example with the flag `false`.
2. `systemctl enable --now readsbstats-vdl2.service` — start the listener.

```bash
sudo sed -i 's/RSBS_VDL2_ENABLED=false/RSBS_VDL2_ENABLED=true/' /etc/readsbstats/vdl2.env
sudo systemctl restart readsbstats-web          # web picks up the shared flag
sudo systemctl enable --now readsbstats-vdl2    # start the ingest listener
```

The web surfaces gate on **runtime availability** (`/api/health` → `vdl2.available`),
so the Messages tab / History "Has ACARS" filter appear only once `vdl2.db` is
actually reachable — a missing/corrupt DB shows an explicit "unavailable" state
and `/api/vdl2/*` return `503`, never a broken page or a silent no-op.

**Integrity:** the ingest service writes a `.vdl2_dirty_shutdown` sentinel next to
`vdl2.db` and clears it on a clean stop; if it's present at startup (unclean
shutdown) it runs `PRAGMA quick_check` before writing and refuses to write on
failure. Retention prune runs in batches so the first big cleanup can't starve
inserts. Watch ingest health via the periodic summary line:
`journalctl -u readsbstats-vdl2 | grep 'vdl2 ingest:'`.

**Decoder (run separately, owns the SDR).** Feed line-delimited JSON over UDP
to the listener (default `127.0.0.1:5556` — 5555 is left free for SpyServer so
the ingest listener can keep running while the SDR is handed to SpyServer):

```bash
# vdlm2dec on an Airspy Mini, 4 PL channels, linearity gain 14:
vdlm2dec -g 14 -j 127.0.0.1:5556 136.725 136.775 136.875 136.975
```

> Verify the exact `-j` / `-i` flag syntax against your `vdlm2dec` build's help
> output — flags have drifted across versions. Only one process may hold the
> Airspy at a time (no SpyServer/SDR++ against the same device concurrently).

**Switching decoders.** `dumpvdl2` cannot drive the Airspy Mini (fixed sample
rate), but if you move to a compatible SDR, set `RSBS_VDL2_DECODER=dumpvdl2` and
point it at the same port:
`dumpvdl2 --output decoded:json:udp:address=127.0.0.1,port=5556 …`. No
readsbstats code change is required (a per-decoder normalizer handles the JSON).

**Retention.** The ingest service prunes messages older than
`RSBS_VDL2_RETENTION_DAYS` (default 90) every hour. Message volume is modest
(thousands–tens of thousands/day at a busy site); at ~1 KB/row stored that's
well within the Pi's `/mnt/ext` headroom. Set `0` to keep everything and manage
size yourself.

```bash
# VDL2 ingest log / status
journalctl -u readsbstats-vdl2 -n 30
systemctl is-active readsbstats-vdl2
```

To disable entirely: `systemctl disable --now readsbstats-vdl2` and unset
`RSBS_VDL2_ENABLED` on `readsbstats-web`. The tab, API, and ingest all
disappear; `history.db` is unaffected.

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

## Storage and retention

The `positions` table is the dominant on-disk consumer — every five-second
poll appends one row per visible aircraft. As a rough Pi-4 sizing rule
measured against a developer database with ~4.2 M `positions` rows in
826 MB: **expect roughly 200 MB per million `positions` rows** including
indexes and WAL overhead. A continuously-running receiver with ~50
concurrent aircraft visible writes on the order of 30 M positions per
month — about 6 GB raw on disk before any rollup.

`RSBS_RETENTION_DAYS=0` (keep forever) is the default. For most home
installations on a 32 GB or larger SSD this is fine, and full history is
the feature most users want. Operators who run on smaller storage, or who
want to bound growth, set a non-zero value:

```ini
# /etc/readsbstats.env
Environment="RSBS_RETENTION_DAYS=365"
```

After restarting `readsbstats-collector`, the next purge cycle drops
`positions` rows older than the cutoff. **The first purge after enabling
retention on a long-running database can hold the SQLite write lock for
several minutes** — schedule the restart during quiet hours, and verify
with `journalctl -u readsbstats-collector -f` that purge completes before
walking away. Subsequent purges are incremental and quick.

Flight-level aggregates in the `flights` table (per-flight max altitude,
max speed, max distance, primary source, first/last seen) **persist
independently** of any `positions` purge — historical Stats / Aircraft
pages keep their summary numbers even after the per-position fixes age
out. Only the position log on the Flight page and the per-flight chart
endpoints lose detail.

See `docs/configuration.md` for the full list of `RSBS_RETENTION_*`
tunables (cutoff days, batch size, min positions to keep).

Two-tier rollup (keep raw N days, LTTB-downsample older flights to a
few hundred points each) is on the roadmap but not yet implemented — the
current retention strategy is hard-cutoff only.

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
- **Optional bearer-token gate.** Setting
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
boundary on every photo emission, so cached
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
