# Integrations

## Telegram notifications

### Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your chat ID: message the bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `result[0].message.chat.id`.
3. Set the environment variables on the Pi:

```bash
systemctl edit readsbstats-collector
```

```ini
[Service]
Environment="RSBS_TELEGRAM_TOKEN=123456:ABCdef..."
Environment="RSBS_TELEGRAM_CHAT_ID=987654321"
Environment="RSBS_SUMMARY_TIME=21:00"
Environment="RSBS_TELEGRAM_UNITS=metric"
Environment="RSBS_TELEGRAM_BASE_URL=http://homepi.local/stats"
```

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
- `/help` — list available commands

### Collector failure alert

`notify-telegram@.service` fires automatically via systemd `OnFailure=` when the collector permanently fails (after exhausting its restart budget). The alert includes the last 30 lines of `systemctl status`. Requires Telegram env vars set in `/etc/readsbstats/readsbstats.env`.

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

Three real-time filters applied in the collector before each position is written:

1. **Hard-limit** — `gs` nulled if it exceeds `RSBS_MAX_GS_CIVIL` (750 kts) for civil aircraft, or `RSBS_MAX_GS_MILITARY` (1800 kts) for military/unknown.
2. **Cross-validation** — `gs` nulled if it deviates from the position-derived implied speed by more than `RSBS_MAX_GS_DEVIATION` (100 kts), when dt ≥ 30 s to the previous accepted position.
3. **MLAT acceleration** — for MLAT positions only, `gs` nulled if the rate of change exceeds `RSBS_MAX_GS_ACCEL` (8.0 kts/s). Catches single-sample multilateration spikes. ADS-B positions are not filtered.

The position itself is always retained — only the `gs` field is set to NULL.

**One-time historical cleanup:**

```bash
# Hard-limit and cross-validation
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_bad_gs.py          # dry-run
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_bad_gs.py --apply

# MLAT acceleration spikes
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_mlat_gs_spikes.py          # dry-run
/opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/purge_mlat_gs_spikes.py --apply
```
