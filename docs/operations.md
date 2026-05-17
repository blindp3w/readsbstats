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

The collector polls every 60 s and stores 43 metrics in `receiver_stats`. The `/metrics` page displays 11 time-series charts with automatic downsampling (raw ≤24h, 5-min ≤7d, 15-min ≤30d, 1-hour ≤90d).

## Polar range plot tuning

The plot auto-scales ring spacing to your actual max detection range. To adjust angular bucket size, edit `BUCKET_DEG` in `web.py` → `api_stats_polar`:

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
