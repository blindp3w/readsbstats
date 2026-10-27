# MCP server (LLM access)

`readsbstats-mcp` is an opt-in service that lets an LLM client query the receiver over the Model Context Protocol. A client can search flight history, read statistics, see live aircraft and receiver health, and add or remove watchlist entries. It is a separate process, off by default. The design is in [ADR-0022](decisions/0022-llm-access-mcp.md).

The server speaks MCP over Streamable HTTP (stateless, JSON responses) on port 8765. It binds to `127.0.0.1` by default.

## How it is protected

- **Separate process, no database access.** The unit sets `InaccessiblePaths` on the data directory, and the package never imports `database` or `api`. It runs as its own dynamic user (`DynamicUser=yes`), not as the `readsbstats` user of the collector and web, so a compromised MCP process cannot open their open files through `/proc` or signal them.
- **No shared secrets.** The unit reads its own `/etc/readsbstats/mcp.env`, not `readsbstats.env`, so it never gets the Telegram token. The tokens file is read by systemd as root and handed to the service as a credential.
- **Data only through the local web API.** The server calls `http://127.0.0.1:8080/api/...` through a narrow client. The client knows a fixed list of routes. Path parameters are validated, the final URL must match the route allowlist before it is sent, and redirects are not followed.
- **One bearer token per client, with scopes.** Scopes are `read` and `watchlist` (`watchlist` implies `read`). The tokens file stores only SHA-256 hashes. With no valid entries, every request gets 401.
- **Per-client limits, charged per tool call.** Defaults: 30 read calls per minute, 10 watchlist writes per hour, 20 photo lookups per hour. A global cap on concurrent API calls (default 2) returns "server busy" when all slots are taken.
- **Watchlist guards.** A `callsign_prefix` must be at least 3 characters. Additions stop at `RSBS_MCP_WATCHLIST_MAX` entries. Every accepted write is logged with the client name, and a removed entry is logged in full so it can be restored.
- **No absolute coordinates.** Results contain no receiver position, no flight bounding boxes and no track or aircraft positions; aircraft are given only as distance and bearing from the receiver. A client that correlates those with public flight-tracking data (callsign and time give a position) can still derive the receiver's location, so treat clients as trusted with the approximate location. Feeding public networks already discloses it roughly.
- **Shaped responses.** Each tool builds its result from an output allowlist. Free text from third parties (callsigns, airport names, photographers, labels) is cleaned and length-capped. Results are capped at about 48 KB. The server instructions tell the model that result values are data, not instructions.
- **Host and Origin checks.** A request whose `Host` header is not allowed gets 421. A request carrying a browser `Origin` header gets 403. Loopback host names are always allowed.

## Enable it

**1. Create a token on the client host.** Generate the token where the client runs, store it in the client's secret store, and compute its hash. Only the hash goes to the receiver.

```bash
TOKEN=$(python3 -c 'import secrets; print("rsbs_mcp_" + secrets.token_urlsafe(32))')
printf %s "$TOKEN" | sha256sum            # on macOS: shasum -a 256
```

**2. Register the hash on the receiver.** Run the token CLI as root:

```bash
sudo /opt/readsbstats/venv/bin/python /opt/readsbstats/scripts/mcp_token.py \
  add <name> --scopes read --sha256 <hash>
```

Use `--scopes read,watchlist` if the client may edit the watchlist. The name is 1 to 32 characters of `a-z`, `0-9` and `-`.

Other commands:

- `mcp_token.py new <name> --scopes read` generates a token on the receiver and prints it once. It refuses to print unless stdout is a terminal. Use it only when the client cannot generate its own token.
- `mcp_token.py list` prints each client's name and scopes.
- `mcp_token.py revoke <name>` removes a client.

The tokens file is `/etc/readsbstats/mcp-tokens` (mode `0600`, owner `root:root`). It is created on the first `add`. Each line is `<name> <scopes> <sha256>`. The file holds hashes only. The service does not read it directly: systemd reads it as root (`LoadCredential=`) and hands the service a private read-only copy, which `RSBS_MCP_TOKENS_FILE` points at. The unit therefore fails to start until the file exists, so create the first token before enabling the service. The CLI's `--file` defaults to the literal `/etc/readsbstats/mcp-tokens`, the source of the credential, not to `RSBS_MCP_TOKENS_FILE`.

**3. For clients on another host.** Settings go in `/etc/readsbstats/mcp.env` (copy `systemd/readsbstats-mcp.env.example`; the file is optional and holds no secrets). Set:

```ini
RSBS_MCP_HOST=<receiver-address>
RSBS_MCP_ALLOWED_HOSTS=<receiver-address>:8765
```

`RSBS_MCP_HOST` is the listen address. `RSBS_MCP_ALLOWED_HOSTS` lists the `host:port` values that clients send in the `Host` header, comma-separated. Then firewall the port so that only the intended clients can reach it. Clients that connect through an SSH tunnel (below) need neither setting.

**4. Start the service.**

```bash
sudo systemctl enable --now readsbstats-mcp
```

The installer copies the unit but does not enable it. The service runs as its own dynamic user. After any token or settings change, restart the service: `sudo systemctl restart readsbstats-mcp`. The tokens file is read only at startup.

## Connect a client

The endpoint is `http://<host>:8765/mcp`, with the header `Authorization: Bearer <token>`.

Generic form, as used by most agent frameworks:

```yaml
url: http://<host>:8765/mcp
headers:
  Authorization: "Bearer <token>"
```

Claude Code over an SSH tunnel. The tunnel keeps port 8765 off the LAN:

```bash
ssh -N -L 8765:127.0.0.1:8765 <user>@<receiver>
claude mcp add --transport http readsbstats http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer <token>"
```

Keep the tunnel open while the client runs. The request's `Host` header is `127.0.0.1:8765`, which is allowed without `RSBS_MCP_ALLOWED_HOSTS`.

## Tools

The server exposes 19 tools. Each returns JSON.

- Times are the receiver's local time, as ISO 8601 strings with a UTC offset.
- Airports are 4-letter ICAO codes (`EPWA`), not city names.
- Callsign, registration and aircraft-type filters are prefixes or exact codes, not free text.
- Flight ids come from `search_flights` or `aircraft_history`.
- `stats` accepts only `24h`, `7d`, `30d`, `90d`, `today`, `yesterday`, a single day `YYYY-MM-DD`, or `all`; anything else is refused, so that repeated questions hit the response cache. `receiver_metrics` periods go up to 7 days.
- Scope: the `read` scope covers every tool except `watchlist_add` and `watchlist_remove`, which need `watchlist`.
- Budgets: each call costs one token from the client's budget. `aircraft_photo` uses the photo budget, the two watchlist writes use the write budget, and the rest use the read budget. A call counts even if it changes nothing.
- A watchlist alert fires on the aircraft's next new flight.

| Tool | Answers | Key parameters | Scope |
|---|---|---|---|
| `search_flights` | Flights by first sighting, filtered by callsign, registration, Mode-S address, type, airports, flag, squawk or position source. Total plus up to 50 rows. | `period` (default `24h`; also `6h`, `7d`, `today`, `YYYY-MM-DD`, `YYYY-MM-DD..YYYY-MM-DD`, `all`), `callsign`, `registration`, `icao_hex`, `aircraft_type`, `origin`, `dest`, `airport`, `flags`, `squawk`, `source`, `sort_by`, `sort_dir`, `limit` (1-50), `offset` | read |
| `get_flight` | One flight in detail, plus up to 10 other recent flights of the same aircraft. | `flight_id` | read |
| `flight_profile` | Altitude, speed, heading and climb-rate samples along one flight. Downsampled; no positions. | `flight_id`, `points` (2-100, default 50) | read |
| `aircraft_history` | Everything seen of one aircraft: registration, type, flags, country and its flights, newest first. | `icao_hex`, `limit`, `offset` | read |
| `airline_flights` | Flights whose callsign starts with an airline's ICAO code, newest first. | `prefix` (2-3 characters, e.g. `LOT`), `limit`, `offset` | read |
| `type_flights` | Flights of one aircraft type, newest first. | `aircraft_type` (e.g. `B738`), `limit`, `offset` | read |
| `aircraft_photo` | Link to a photo of one aircraft, or of its type when none exists. The first request can take up to 25 s. | `icao_hex` | read (photo budget) |
| `flagged_aircraft` | Military, interesting or anonymous-address aircraft, one row per aircraft with flight count and first and last sighting. | `kind` (`military`, `interesting`, `anonymous`), `sort_by`, `sort_dir`, `limit`, `offset` | read |
| `live_aircraft` | Aircraft tracked now, nearest first: distance and bearing from the receiver, altitude, speed, heading, route. No coordinates. | `limit` (1-200, default 50) | read |
| `sky_at` | Aircraft in the sky at a moment: every aircraft seen in the 10 minutes before it, with its last known distance and bearing from the receiver (no coordinates). Only the last few hours are available. | `time` (`now`, `YYYY-MM-DD` or a local ISO time), `limit` | read |
| `stats` | Totals, top airlines, types, routes and airports, busiest hours, new aircraft, furthest flight and more. | `period` (default `24h`; `7d`, `30d`, `90d`, `today`, `yesterday`, `YYYY-MM-DD` or `all`), `sections` (optional list) | read |
| `records` | All-time records: furthest, fastest, highest and longest flights. | none | read |
| `daily_counts` | Flights per day, newest first. Days with no flights are absent. | `days` (1-365, default 30) | read |
| `range_by_bearing` | Maximum detection range in nautical miles for each 10-degree sector (0 = north), all time. | none | read |
| `receiver_status` | Receiver health in one call: web API status, health checks with severities, and feeder service states. | none | read |
| `receiver_metrics` | Per metric: min, average, max, last and up to 48 sampled points. | `metrics` (1-6 names, default `signal`, `noise`, `messages`), `period` (default `24h`, up to 7 days) | read |
| `watchlist_list` | The watchlist entries. | none | read |
| `watchlist_add` | Adds an entry: `icao` (6 hex digits), `registration`, or `callsign_prefix` (3-8 characters). An existing entry is returned, not duplicated. | `match_type`, `value`, `label` (optional) | watchlist |
| `watchlist_remove` | Removes one entry by type and exact value. Returns the removed entry. | `match_type`, `value` | watchlist |

## Operations

- **Logs:** `journalctl -u readsbstats-mcp -f`. Accepted watchlist writes are logged as `mcp write client=<name> tool=<tool> ...`, and a removed entry is logged in full. A rejected request (missing or wrong token) is logged at most once a minute, with the client address and never the token.
- **Startup:** the log lists the clients that were loaded. With no tokens configured the service still starts, logs an error, and rejects every request. If the tokens file is group- or world-writable, the service refuses to start (`chmod go-w` fixes it). With no tokens file the unit does not start at all, because systemd cannot load the credential.
- **Updates:** `update.sh` restarts `readsbstats-mcp` only if it is already active.
- **Resource limits:** `CPUQuota=25%`, `MemoryMax=256M`, `Nice=10`. The process is idle when no client is calling it.
- **Rotation and revocation:** `mcp_token.py revoke <name>`, or `add` a new hash, then `sudo systemctl restart readsbstats-mcp`.
- **Turn off:** `sudo systemctl disable --now readsbstats-mcp`.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| HTTP 401 with `{"error": "unauthorized"}` | No `Authorization` header, a token whose hash is not in the tokens file, or an empty tokens file. | Check the header reads `Bearer <token>`. Recompute the hash on the client and compare it with the third field of the client's line in the tokens file. Restart after changes. |
| HTTP 421 | The `Host` header is not allowed. | Add the `host:port` value to `RSBS_MCP_ALLOWED_HOSTS`, then restart. |
| HTTP 403 | The request carries an `Origin` header, as browsers send. | Use a non-browser MCP client. |
| "this token lacks the 'watchlist' scope" | The client's token is read-only. | Revoke the client and `add` it again with `--scopes read,watchlist`. |
| "rate limit for read calls reached; retry in N s" (`write`, `photo` likewise) | The client used up its budget. | Wait. The server instructions tell the model to stop calling tools. |
| "server busy; retry in a few seconds" | All concurrent API slots stayed busy for 5 s. | Retry. If it persists, check `journalctl -u readsbstats-web`. |
| "readsbstats API is unreachable" or "slow right now" | The web service is down or busy. | `systemctl status readsbstats-web`. |
| "watchlist is full" or "callsign_prefix must be at least 3 characters" | Guard in the watchlist tools. | Remove an entry, or use a longer prefix. |
| Unit fails with "Failed to set up credentials" | `/etc/readsbstats/mcp-tokens` does not exist. | Register a client with `mcp_token.py add`, then start again. |
| Service exits at start with "group- or world-writable" | Wrong mode on the tokens file. | `sudo chmod 600 /etc/readsbstats/mcp-tokens`, then start again. |

## Configuration

All ten variables are read by `readsbstats-mcp` only. Put them in `/etc/readsbstats/mcp.env` (template: `systemd/readsbstats-mcp.env.example`), not in `readsbstats.env`, and restart the service. See [Configuration → MCP server](configuration.md#mcp-server-opt-in).

| Variable | Default | Description |
|---|---|---|
| `RSBS_MCP_HOST` | `127.0.0.1` | Listen address. Keep it on loopback unless clients connect from another host. |
| `RSBS_MCP_PORT` | `8765` | Listen port (min `1`). |
| `RSBS_MCP_ALLOWED_HOSTS` | _(empty)_ | Extra accepted `Host` header values (`host:port`), comma-separated. Loopback names are always accepted. |
| `RSBS_MCP_TOKENS_FILE` | `/etc/readsbstats/mcp-tokens` | Per-client token file (name, scopes, SHA-256 hash). The unit sets it to the credential copy (`%d/mcp-tokens`); do not set it in `mcp.env`. For development runs only. |
| `RSBS_MCP_API_URL` | `http://127.0.0.1:8080` | Local web API the server calls. Must be `http://` on a loopback host, with no path. |
| `RSBS_MCP_READ_PER_MIN` | `30` | Read tool calls per client per minute (min `1`). |
| `RSBS_MCP_WRITE_PER_HOUR` | `10` | Watchlist write calls per client per hour (min `1`). |
| `RSBS_MCP_PHOTO_PER_HOUR` | `20` | Photo lookups per client per hour (min `1`). |
| `RSBS_MCP_CONCURRENCY` | `2` | Concurrent API calls across all clients (min `1`). |
| `RSBS_MCP_WATCHLIST_MAX` | `100` | Watchlist size cap for additions made through MCP (min `1`). |
