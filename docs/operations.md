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

Every run first snapshots `history.db` with `VACUUM INTO` to
`history.db.backup.<YYYYMMDD_HHMMSS>` next to the database and keeps the 3 most
recent. On a large database this is the slowest step (~10 minutes for 1.6 GB on
a Pi 4 with a USB disk) and needs free space roughly equal to the DB size.

After a restart the web server's cache prewarmer rebuilds the map caches, so the
first heatmap/coverage request can take a while to render — that is not an error.

## Data directory

`history.db` and `vdl2.db` live in one directory, set by `RSBS_DATA_DIR` (default `/var/lib/readsbstats`). `RSBS_DB_PATH` defaults to `$RSBS_DATA_DIR/history.db` and `RSBS_VDL2_DB_PATH` to `$RSBS_DATA_DIR/vdl2.db`; an explicit value for either still wins. Before v2.32.0 the path was hard-coded to a mount point on the reference Pi's USB disk.

`RSBS_DATA_DIR` is a deploy-time setting. Put it in `/etc/readsbstats/readsbstats.env`; it is not shown in `/api/settings`. Allowed characters are `A-Z a-z 0-9 . _ - /`, the path must be absolute, and `..` is refused.

The units in `systemd/` contain a `@DATA_DIR@` placeholder. `scripts/install.sh` and `scripts/update.sh` render it into `/etc/systemd/system/` (shared helper `scripts/_deploy_lib.sh`), filling in the sandbox `ReadWritePaths=`, `Environment=RSBS_DATA_DIR=`, and `RequiresMountsFor=`. Copying a unit by hand with `cp` no longer works, because the placeholder is left unrendered.

The data directory is resolved in this order:

1. `RSBS_DATA_DIR` in `readsbstats.env`.
2. Migration for existing installs that have no setting yet: the `ReadWritePaths=` of the installed collector unit is appended once to `readsbstats.env`, with a `# Added by update.sh (v2.32.0)` comment and `RSBS_DATA_DIR=<dir>`.
3. The default, `/var/lib/readsbstats`.

`update.sh` prints `Data dir: <dir> (source: env|migrated|default)`. If an installed collector unit exists but `<dir>/history.db` does not, it aborts before changing anything.

`RequiresMountsFor=` makes the collector, web, VDL2 ingest, updater and both dbcheck services wait for the mount that holds the data directory. This matters for a separate disk, such as a USB drive in `/etc/fstab` with `nofail`. When the directory is on the root filesystem, the directive has no effect.

**Moving the data directory.** Stop the services, move the files (including `-wal`, `-shm` and the sentinels), set the new path, and re-run `update.sh`:

```bash
sudo systemctl stop readsbstats-collector readsbstats-web readsbstats-vdl2
sudo mkdir -p /srv/readsbstats
# A glob with no matches makes mv print an error for that pattern; that is harmless.
sudo mv /var/lib/readsbstats/history.db* /var/lib/readsbstats/vdl2.db* \
        /var/lib/readsbstats/.*_shutdown /srv/readsbstats/
sudo chown -R readsbstats:readsbstats /srv/readsbstats
echo 'RSBS_DATA_DIR=/srv/readsbstats' | sudo tee -a /etc/readsbstats/readsbstats.env
sudo bash /opt/readsbstats/scripts/update.sh
```

`update.sh` re-renders the units for the new path and restarts the services. The MCP unit gets `InaccessiblePaths=-` on the same directory, so it cannot read the data either way.

**One-shot scripts** run by hand do not get the units' environment. Pass the setting on the command line, or give an explicit `--db`:

```bash
sudo -u readsbstats env RSBS_DATA_DIR=/srv/readsbstats \
    /opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/check_db.py
```

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
systemctl disable --now readsbstats-collector readsbstats-web \
  readsbstats-updater.timer readsbstats-dbcheck.timer readsbstats-dbcheck-full.timer
systemctl disable --now readsbstats-vdl2   # opt-in; skip if never enabled (decoder pipeline: see "Turning VDL2 off")
systemctl disable --now readsbstats-mcp    # opt-in MCP server; skip if never enabled (see docs/mcp.md)
```

Settings live in `/etc/readsbstats/readsbstats.env`, read by `readsbstats-collector`, `readsbstats-web` and `notify-telegram@` — edit it, then restart the services (see [Configuration](configuration.md)).

## RRD history import

To backfill `receiver_stats` from existing graphs1090 RRD files (needs `rrdtool`: `apt install rrdtool`):

```bash
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/import_rrd.py \
  --rrd-dir /path/to/dump1090-localhost [--db PATH] [--dry-run]
```

`--rrd-dir` is the extracted graphs1090 directory containing the `.rrd` files; `--dry-run` shows what would be imported without writing.

Maps 20 RRD files to 21 receiver metric columns. Uses `INSERT OR IGNORE` to preserve finer-grained data across multiple resolution tiers.

## Receiver metrics

Enable metrics collection from `/run/readsb/stats.json`:

```ini
# /etc/readsbstats/readsbstats.env
RSBS_METRICS_ENABLED=1
```

Then `sudo systemctl restart readsbstats-collector`. The collector polls every 60 s and stores 43 metrics in `receiver_stats`. The `/metrics` page displays 10 time-series charts with automatic downsampling (raw ≤24h, 5-min ≤7d, 15-min ≤30d, 1-hour ≤90d, 4-hour beyond 90d).

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

**Decoder (run separately, owns the SDR).** [dumpvdl2](https://github.com/szpajder/dumpvdl2)
is the recommended, supported decoder and the code default
(`RSBS_VDL2_DECODER=dumpvdl2`; before 2.29.0 the default was `vdlm2dec`). Feed
line-delimited JSON over UDP to the listener (default
`127.0.0.1:5556` — 5555 is left free for SpyServer so the ingest listener can
keep running while the SDR is handed to SpyServer):

```bash
dumpvdl2 --output decoded:json:udp:address=127.0.0.1,port=5556 …
```

It decodes ACARS plus the ATN/OSI classes vdlm2dec can't (notably **CPDLC**
controller–pilot messages and the ATN Context-Management logon that supplies the
aircraft's reported route), the H1 `sublabel`/`mfi` fields, ADS-C positions, and
reports per-frame signal level (the Metrics signal/SNR charts). A per-decoder
normalizer handles the JSON; no other readsbstats change is needed.
`dumpvdl2` needs a sample rate that is a multiple of 105000 sym/s, so it requires
either a compatible SDR or an external resampler ahead of the Airspy Mini's fixed
rates — see [Running dumpvdl2 on an Airspy Mini](dumpvdl2-airspy-mini.md) for a
tested IQ‑resampling recipe.

**vdlm2dec (legacy, limited).** [vdlm2dec](https://github.com/TLeconte/vdlm2dec)
works with `RSBS_VDL2_DECODER=vdlm2dec` (set it in `/etc/readsbstats/vdl2.env` and
restart `readsbstats-vdl2`) but is ACARS only: no signal level or SNR (the Metrics
charts stay hidden), no `sublabel`/`mfi` handling, no CPDLC, no CM logon routes and
no ADS-C positions. If the decoder setting does not match the decoder's JSON
dialect, datagrams arrive but nothing is stored; after 200 datagrams with no
stored record the ingest logs a one-time warning to that effect.

```bash
# vdlm2dec on an Airspy Mini, 4 PL channels, linearity gain 14:
vdlm2dec -g 14 -j 127.0.0.1:5556 136.725 136.775 136.875 136.975
```

> Verify the exact `-j` / `-i` flag syntax against your `vdlm2dec` build's help
> output — flags have drifted across versions. Only one process may hold the
> Airspy at a time (no SpyServer/SDR++ against the same device concurrently).

**One-time backfill after upgrading to 2.26.0, 2.27.0 and 2.28.0 (dumpvdl2 feeds).** Rows ingested
before 2.26.0 are missing the H1 `sublabel`/`mfi` and the XID position and
destination, because the old normalizer dropped them. Rows ingested before
2.27.0 also have ATN Context-Management logons stored without content (`CM`
label, body and `dsta` missing). New rows are fixed automatically. To fill older
rows, run the backfill once after each update. It only fills NULL columns from
the stored `raw` JSON, and it can run while `readsbstats-vdl2` is live (no
service stop needed). A second run updates nothing. Upgrading to 2.27.0 on a
database already backfilled for 2.26.0 only touches the CM rows. Upgrading to
2.28.0 adds the `more` flag and a pass that hides earlier blocks of multi-block
ACARS messages (`superseded`). The same command runs both, and it is safe to
re-run. Until the backfill has run, older duplicate blocks stay visible in the
feed. Expect a few minutes on a Raspberry Pi 4 for a 90-day database, because the
2.28.0 pass reads every ACARS row once:

```bash
sudo -u readsbstats /opt/readsbstats/venv/bin/python \
    /opt/readsbstats/scripts/backfill_vdl2_dumpvdl2.py \
    --db /var/lib/readsbstats/vdl2.db   # your RSBS_DATA_DIR/vdl2.db
# add --dry-run to count what would change without writing
# add --chunk N to change the rows per transaction (default 2000)
```

`--dry-run` skips the supersede pass (it depends on `more` being filled first)
and reports `"superseded": null`.

Skipping it is mostly harmless: unfilled rows simply age out with retention, but
until then duplicate multi-block messages from before 2.28.0 keep showing in the
feed. The
`--db` flag is required under `sudo`, because `/etc/readsbstats/vdl2.env` is not
loaded in that context.

**Retention.** The ingest service prunes messages older than
`RSBS_VDL2_RETENTION_DAYS` (default 90) once at startup and then every
`RSBS_VDL2_PURGE_INTERVAL` seconds (default 3600). Message volume is modest
(thousands–tens of thousands/day at a busy site); at ~1 KB/row stored that's
well within the data disk's headroom. Set `0` to keep everything and manage
size yourself.

**Indexes built in the background.** Two stats indexes, `idx_vdl2_label_icao` and
`idx_vdl2_flight`, need a full pass over the table, so the ingest service builds
them on its prune thread right after the startup prune (never on the web or
startup path; a failed build is retried on the next prune tick). Until they exist the Stats card's top-labels and top-airlines aggregates
read the whole table: 2–4 minutes on a cold cache on a Raspberry Pi 4 with a USB
disk. Results are cached, and concurrent requests share one computation. The
build runs once and is a no-op afterwards.

**Malformed input.** The ingest drops (and counts as malformed) any datagram line
whose JSON has `NaN`/`Infinity`/out-of-range numbers or extreme nesting, instead
of crashing. A failed database open in the prune thread is logged and retried on
the next tick.

```bash
# VDL2 ingest log / status
journalctl -u readsbstats-vdl2 -n 30
systemctl is-active readsbstats-vdl2
```

### Turning VDL2 off

VDL2 is frozen as of 2.29.0 ([ADR-0021](decisions/0021-vdl2-frozen.md)): the code
stays, opt-in and supported as-is, and turning it off is safe. With
`RSBS_VDL2_ENABLED=false` the web server never opens or ATTACHes `vdl2.db`, the
`/api/vdl2/*` router is not registered (unknown `/api` paths return a JSON `404`),
and every VDL2 surface in the SPA hides itself (nav item, Messages tab, Stats and
Metrics cards, ACARS panels, the History "Has ACARS" filter and the map ACARS
layer). `history.db` is never touched by VDL2, and no alert depends on it.

1. Set `RSBS_VDL2_ENABLED=false` in `/etc/readsbstats/vdl2.env`. The web unit reads
   that file after `readsbstats.env`, so a value there wins; the ingest unit reads
   only `vdl2.env`.
2. `sudo systemctl restart readsbstats-web`
3. `sudo systemctl disable --now readsbstats-vdl2` (started with the flag off, the
   service sends `READY` and exits cleanly, so a leftover enabled unit is harmless).
4. Stop the external decoder pipeline (`airspy_rx | … | dumpvdl2`, or `vdlm2dec`).
   No unit in this repo manages it. Stopping it frees the SDR.
5. `vdl2.db` (with `-wal`, `-shm` and `.vdl2_dirty_shutdown`) stays on disk
   untouched. Nothing opens it while the flag is off, and retention pruning stops
   (it runs in the ingest service). To reclaim the space delete
   `$RSBS_DATA_DIR/vdl2.db*` and `$RSBS_DATA_DIR/.vdl2_dirty_shutdown` (default `/var/lib/readsbstats`); to shrink a kept archive run `VACUUM` on it
   offline while the services are stopped.

To turn it back on, set the flag to `true`, restart `readsbstats-web`, run
`systemctl enable --now readsbstats-vdl2` and start the decoder. On its first start
the ingest service builds the two extra indexes in the background (see above).

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
snapshot_db('/var/lib/readsbstats/history.db')  # your RSBS_DATA_DIR/history.db
"
```

## Storage and retention

The `positions` table is the dominant on-disk consumer — every five-second
poll appends one row per visible aircraft. Measured on a production Pi 4
(2026-10-24, schema v6, a busy Warsaw-area receiver): **≈ 89 bytes per
`positions` row** including indexes, and growth of **≈ 300 MB/month** before
retention kicks in (about 3.4 M rows/month). With
`RSBS_RETENTION_DAYS=180`, `history.db` reached a plateau of 1.82 GB
(20.5 M position rows, 228 k flights). Both figures are traffic-dependent, so
scale them to your receiver.

`RSBS_RETENTION_DAYS=0` (keep forever) is the default. For most home
installations on a 32 GB or larger SSD this is fine, and full history is
the feature most users want. Operators who run on smaller storage, or who
want to bound growth, set a non-zero value:

```ini
# /etc/readsbstats/readsbstats.env
RSBS_RETENTION_DAYS=365
```

After restarting `readsbstats-collector`, the next purge cycle drops
`positions` rows older than the cutoff. The purge deletes in batches of
50 000 rows, each its own transaction with a 200 ms pause between batches, so
polling and web readers keep working. **The first purge after enabling
retention on a long-running database can still take several minutes** —
verify with `journalctl -u readsbstats-collector -f` that it completes
(`Purge: deleted N expired positions`). It waits until the one-time rollup
backfill has finished. Subsequent purges are incremental and quick.

Retention is not limited to raw fixes: once a flight's positions have all aged
out, the flight row itself is deleted (`RSBS_MIN_POSITIONS`, `last_seen`
before the cutoff, no active session). The History, Statistics and Aircraft
pages and the leaderboards therefore cover only the retention window. Only
the coarse (0.1°) heatmap rollup and the coverage rollup are permanent, so
the all-time heatmap and coverage overlays keep their full history.
Flights that span the cutoff keep their surviving positions and have their
aggregates recomputed. See [Configuration](configuration.md#retention--what-survives-the-horizon).

The retention tunables are `RSBS_RETENTION_DAYS`, `RSBS_PURGE_INTERVAL` and
`RSBS_GRID_FINE_RETENTION_DAYS`; the purge batch size is fixed at 50 000 rows.
Flights with too few positions are governed by `RSBS_MIN_POSITIONS`. See
[Configuration](configuration.md) for details.

Two-tier rollup (keep raw N days, LTTB-downsample older flights to a
few hundred points each) is on the roadmap but not yet implemented — the
current retention strategy is hard-cutoff only.

## Deployment security

readsbstats ships **no authentication and no authorization**. The
trust model is "bind to loopback, sit behind nginx on a trusted LAN."

**What this means in practice:**

- The uvicorn app listens on `127.0.0.1:8080`. nginx proxies `/stats/` to it.
  Anyone who can reach that port (directly, or through nginx) can read every
  flight, aircraft, and metrics endpoint **and** call every mutating endpoint
  (watchlist add/delete — the only mutating endpoints; `/api/settings` is read-only).
- **The `X-Requested-With: XMLHttpRequest` CSRF check is not authentication.**
  It only stops a third-party web page from silently POSTing to the API in a
  logged-in browser. It does not identify, authenticate, or authorize the
  caller — a direct `curl` with that header passes it. Never treat it as access
  control.
- **No bearer-token gate.** `RSBS_API_TOKEN` was removed in v2.30.0: the SPA
  never sent the token, so setting it broke watchlist editing, and read
  endpoints were never gated. If the variable is still set, the web server logs
  a startup warning and ignores it. Use an authenticating reverse proxy for
  access control.
- **`flight_id` path params are integer-typed; `{icao_hex}` path params are
  validated** against `^~?[0-9a-fA-F]{6}$` before any DB or outbound work, and
  feeder `status_path` values are `realpath`-checked under `RSBS_FEEDER_STATUS_ROOT`.
  These are defence-in-depth (bounding side effects), **not** a substitute for
  network-level access control.
- **The optional MCP server** (`readsbstats-mcp`, port 8765) is off by default. When enabled, it is the only way an LLM client should reach the data. It has its own per-client bearer tokens, scopes and limits, and it calls the web API on loopback only. Bind it to loopback or a VPN address, allow the port only from the intended clients, and never expose it to the internet. See [MCP server](mcp.md).

**If you expose the UI beyond a trusted LAN**, put an authenticating layer in
front of nginx — HTTP basic auth, an OAuth2 reverse proxy (e.g. oauth2-proxy),
or a VPN / Tailscale tailnet. Do not publish `127.0.0.1:8080` or the nginx
`/stats/` location to the public internet without one.

**Outbound HTTP** (photo + route enrichment) is centralised in `http_safe.py`
with an SSRF guard (HTTPS-only, globally-reachable-IP-only, multicast-blocked,
redirect-blocked). Provider photo URLs are additionally checked against
per-source CDN host allowlists before they are cached, and at the API response
boundary on every photo emission, so cached
off-allowlist URLs never reach the SPA regardless of `RSBS_PHOTO_HOST_ENFORCE`.
The image-host and link-host allowlists are separate (HTTPS only; a host
matches itself and its subdomains, `src/readsbstats/photo_sources.py`).
Image URLs (`thumbnail_url`, `large_url`) must be on `plnspttrs.net`,
`planespotters.net`, `airport-data.com`, `hexdb.io` or
`upload.wikimedia.org`; link URLs (`link_url`) accept the same hosts plus
`en.wikipedia.org`, which serves article pages, not images.

## Database integrity & startup recovery

The collector and the VDL2 ingest write a sentinel next to their database on startup (`.dirty_shutdown`, or `.vdl2_dirty_shutdown` for VDL2) and remove it only on a clean shutdown. If the sentinel is present at the next start (the previous run crashed or lost power), the service runs `PRAGMA quick_check(10)` **before** it writes anything. The collector runs this check before `init_db()` (schema and migrations, which write).

The check runs in a worker thread while systemd waits for the service to start. Every 10 s the service extends systemd's start timeout and updates its status line, visible in `systemctl status`, for example `STATUS=collector: quick_check running, 95 s`. The extension stops after `RSBS_STARTUP_CHECK_MAX_SEC` (default `1800`, minimum `60`; an internal tunable, not in `/api/settings`), so a check that hangs, for example on a dead disk, is still killed by `TimeoutStartSec`. A passing check logs `DB integrity check passed; checkpointing WAL` and `collector: quick_check finished in N s`.

**Stopping during the check.** `systemctl stop` interrupts a running check and returns within seconds. The service exits 0, writes nothing and keeps the sentinel, so the next start runs the check again.

**Fail-closed behaviour** is unchanged. If the check finds corruption, or if `quick_check` cannot run at all, the service notifies systemd and exits without writing: the collector sends a Telegram alert and exits with code `2`; VDL2 ingest exits with code `1`, and its unit's `OnFailure=` sends the generic failure alert. The sentinel is left in place, so the check repeats on every restart until an operator intervenes.

**Start limits.** `Restart=on-failure` with `StartLimitBurst=5` / `StartLimitIntervalSec=120` bounds fast failures such as corruption (exit 2): after about 5 restarts systemd parks the unit in `failed` state, which fires `OnFailure=notify-telegram@…`. A slow check does not reach that limit. Before v2.32.0 the start timeout killed a slow check and the unit looped on it indefinitely.

### Telling the cases apart

```bash
journalctl -u readsbstats-collector -n 100 | grep -E 'start operation timed out|DB CORRUPTION DETECTED'
```

- `start operation timed out` means the start timeout fired. This should not happen any more, except when a check runs past `RSBS_STARTUP_CHECK_MAX_SEC`. A hung disk is the usual cause.
- `DB CORRUPTION DETECTED` means `quick_check` found damage. Use the recovery steps below.

### Manual way out when the check itself is too slow

On a slow disk the check can take minutes (about 3 minutes for a 1.8 GB database on a USB HDD). To run it yourself, stop the unit and run the check outside the unit, which has no CPU or memory limits:

```bash
sudo systemctl stop readsbstats-collector
DB=/var/lib/readsbstats/history.db   # your RSBS_DATA_DIR/history.db
sudo -u readsbstats sqlite3 "$DB" 'PRAGMA quick_check(10);'
```

Only if the output is exactly `ok`, remove the sentinel and start the unit:

```bash
sudo rm -f "$(dirname "$DB")/.dirty_shutdown"
sudo systemctl start readsbstats-collector
```

Any other output means corruption: do not clear the sentinel, and follow the recovery steps.

If you still need different limits on one machine, use a drop-in rather than editing the unit: `sudo systemctl edit readsbstats-collector` (or `readsbstats-vdl2`) writes `/etc/systemd/system/<unit>.d/override.conf`. `update.sh` re-renders the main unit files but never touches drop-ins. The v2.32.0 limits are `CPUQuota=50%` and `MemoryMax=1G` for the collector, and `CPUQuota=50%` and `MemoryMax=768M` for VDL2 ingest. `MemoryMax` counts page cache.

**Recovery steps** (run as root, collector stopped):

```bash
systemctl stop readsbstats-collector
DB=/var/lib/readsbstats/history.db   # your RSBS_DATA_DIR/history.db

# 1. Snapshot the corrupt file first (forensics / second attempt).
cp "$DB" "$DB.corrupt-$(date +%s)"

# 2a. Preferred: restore the most recent good backup — either an update.sh
#     pre-deploy snapshot (history.db.backup.<ts>) or a purge-script
#     snapshot (history.db.backup-<ts>.db).
ls -t "$DB".backup*
cp "$DB".backup<...> "$DB"

# 2b. Or salvage in place with the SQLite recovery tool.
sqlite3 "$DB" ".recover" | sqlite3 "$DB.recovered"
mv "$DB.recovered" "$DB"

# 3. Clear the sentinel so the integrity check passes on next start.
rm -f "$(dirname "$DB")/.dirty_shutdown"

systemctl start readsbstats-collector
```

Verify with `journalctl -u readsbstats-collector -n 30` — a clean start logs
`DB integrity check passed; checkpointing WAL`.
