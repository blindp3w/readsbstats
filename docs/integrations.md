# Integrations

## Telegram notifications

### Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your chat ID: message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `result[0].message.chat.id`.
3. Set the variables once in `/etc/readsbstats/readsbstats.env` — the single settings file read by the collector, the web service and the failure-alert one-shot (`notify-telegram@.service`). `install.sh` creates it (root-owned, mode 0600, from `systemd/readsbstats.env.example`) on a fresh install; if it is missing, copy the example there yourself:

```ini
RSBS_TELEGRAM_TOKEN=123456:ABCdef...
RSBS_TELEGRAM_CHAT_ID=987654321
RSBS_SUMMARY_TIME=21:00
RSBS_TELEGRAM_UNITS=metric
RSBS_TELEGRAM_BASE_URL=http://your-pi.local/stats
```

4. Restart the services so they pick the file up:

```bash
sudo systemctl restart readsbstats-collector readsbstats-web
```

The token and chat ID are needed only once: the failure alerts use the same file, so nothing is duplicated.

Alternatively, `systemctl edit readsbstats-collector` with `Environment="RSBS_TELEGRAM_TOKEN=..."` lines in a `[Service]` drop-in also works for the collector, but the failure-alert unit never sees a drop-in (it has to read the token from the env file), and a value in `readsbstats.env` overrides a drop-in's `Environment=` for the same variable — so keep each variable in one place.

Notifications are disabled when the token or chat ID is not set — fully opt-in.

### What gets sent

- **Military aircraft** — once per ICAO hex on first detection; includes a photo (specific aircraft photo, or type-level fallback with a caption note).
- **Interesting aircraft** — same (government, VIP, air ambulance, special mission per tar1090-db); mutually exclusive with military.
- **Anonymous aircraft (non-ICAO hex)** — once per ICAO hex on first detection. Fires when the 24-bit Mode-S address falls outside every ICAO state-allocated block. Mute with `RSBS_TELEGRAM_ANONYMOUS_ALERT=0`.
- **Watchlist hits** — once per flight when a watched aircraft is first detected; includes photo.
- **Emergency squawk** — once per flight when squawk 7500, 7600, or 7700 is detected.
- **Daily summary** — at `RSBS_SUMMARY_TIME` local time: total flights, unique aircraft, military/interesting/anonymous counts, emergency squawks, furthest/fastest/highest/longest aircraft, busiest hour.

Precedence: military > interesting > anonymous — each flight surfaces under exactly one kind.

### Interactive bot commands

The bot only responds to the configured `RSBS_TELEGRAM_CHAT_ID`.

- `/summary` — on-demand daily summary
- `/status` — aircraft currently in range + today's flight count
- `/watchlist` — show the watchlist entries
- `/watch <icao|reg>` — add an aircraft to the watchlist (a 6-hex value is stored as an ICAO address, anything else as a registration)
- `/unwatch <icao|reg>` — remove an entry (also removes `callsign_prefix` entries added via the web UI)
- `/help` (or `/start`) — list available commands

### Collector failure alert

`notify-telegram@.service` fires automatically via systemd `OnFailure=` when the collector permanently fails (after exhausting its restart budget). The alert includes the last 30 lines of `systemctl status`. Reads `RSBS_TELEGRAM_TOKEN` / `RSBS_TELEGRAM_CHAT_ID` from `/etc/readsbstats/readsbstats.env`; without them the alert is not sent.

### Photo delivery

Photos are downloaded locally and uploaded via `multipart/form-data` to Telegram's `sendPhoto` API — direct hotlinks from Telegram's bot servers are blocked by Planespotters and other sources. Set `RSBS_TELEGRAM_PHOTOS=0` to disable photo enrichment entirely.

---

## Ghost position filtering

ADS-B receivers occasionally decode "ghost" positions — phantom ICAO address collisions or spoofing artefacts that place an aircraft thousands of nautical miles away for a single sample.

**Real-time filter (collector):** Any position whose implied ground speed from the previous accepted position exceeds `RSBS_MAX_SPEED_KTS` (default 2000 kts) is dropped before it reaches the database.

**Recommended readsb settings:**

```
--json-reliable 2
--position-persistence 4
```

**One-time historical cleanup:**

```bash
# Dry-run
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_ghosts.py

# Apply
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_ghosts.py --apply
```

Options: `--db PATH`, `--max-speed N`.

---

## Ground speed filtering

Three real-time filters applied in the collector before each position is written, plus one post-flight pass:

1. **Hard-limit** — `gs` nulled if it exceeds `RSBS_MAX_GS_CIVIL` (750 kts) for civil aircraft, or `RSBS_MAX_GS_MILITARY` (1800 kts) for military/unknown.
2. **Cross-validation** — `gs` nulled if it deviates from the position-derived implied speed by more than `RSBS_MAX_GS_DEVIATION` (100 kts), when dt ≥ 30 s to the previous accepted position.
3. **MLAT acceleration** — for MLAT positions only, `gs` nulled if the rate of change exceeds `RSBS_MAX_GS_ACCEL` (8.0 kts/s). Catches single-sample multilateration spikes. ADS-B positions are not filtered.

When a flight is closed, a fourth, statistical pass runs over its MLAT readings: if the flight has at least `RSBS_MLAT_OUTLIER_MIN` (default 10) MLAT `gs` values, any value above `RSBS_MLAT_OUTLIER_FACTOR` (default 5.0) × the 75th percentile of the flight's own MLAT `gs` values is nulled and the flight's `max_gs` is recomputed. It catches isolated leading spikes the acceleration filter cannot see (no predecessor, or a large time gap). The pass is skipped when that percentile is 0.

The position itself is always retained — only the `gs` field is set to NULL.

**One-time historical cleanup:**

```bash
# Hard-limit and cross-validation
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_bad_gs.py          # dry-run
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_bad_gs.py --apply

# MLAT acceleration spikes + statistical outliers
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_mlat_gs_spikes.py          # dry-run
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_mlat_gs_spikes.py --apply
```

Options for `purge_mlat_gs_spikes.py`: `--db PATH`, `--accel-limit` (default `RSBS_MAX_GS_ACCEL`), `--outlier-factor` (default `RSBS_MLAT_OUTLIER_FACTOR`), `--min-gs-count` (default `RSBS_MLAT_OUTLIER_MIN`, floor 2). `--apply` writes the changes (after an automatic `VACUUM INTO` snapshot; `--i-have-a-backup` skips it).
