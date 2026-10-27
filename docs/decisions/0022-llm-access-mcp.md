# LLM access through a separate MCP server

- Status: ACCEPTED
- Date: 2026-10-27

## Context

The maintainer wants LLM clients to look up flights, read statistics and edit the
watchlist. One is an AI agent on a remote host that reaches the receiver over a VPN;
another may be a coding assistant on the LAN. Clients like these can misbehave: a model
can loop or retry, read text placed in the data as instructions, or ask for far more than
it needs. The receiver is a Raspberry Pi 4 whose SQLite database the collector writes
every 5 s. The setup must therefore prevent unintended writes and protect the Pi from
overload. The server must also be generic: nothing in it may depend on one client, one
agent framework or one network layout.

The web API has no authentication. It is meant for a trusted LAN behind nginx, and
`RSBS_API_TOKEN` was removed in 2.30.0. Its only writes are watchlist add and delete.
Many responses carry the receiver's coordinates (`receiver_lat`/`receiver_lon`, flight
bounding boxes, track points, the FR24 radar code), and the receiver's coordinates are
the operator's home.

## Decision

1. **A separate opt-in service, `readsbstats-mcp`.** It runs `python -m
   readsbstats.mcp_server`, speaks MCP over Streamable HTTP (stateless, JSON responses)
   and listens on port 8765 (`RSBS_MCP_HOST`/`RSBS_MCP_PORT`, default `127.0.0.1`). It
   runs in its own process and has no access to the database: the unit sets
   `InaccessiblePaths=-/mnt/ext/readsbstats`, and the package never imports `database`
   or `api.*`.
2. **Data only through the local API, via a narrow client.** Every call goes to
   `http://127.0.0.1:8080/api/...`, with no `/stats` prefix, through a new
   `http_safe.loopback_request()`. That function accepts only http on a loopback host,
   follows no redirects and caps the body by streaming it. On top of it, the server
   knows a fixed set of named route templates:
   - every path parameter is validated: flight id is a JSON integer > 0; ICAO hex is
     `~?[0-9a-f]{6}`; aircraft type is `[A-Z0-9]{2,4}`; airline prefix is
     `[A-Z0-9]{2,3}`;
   - query values go through `params=`;
   - the built request's raw path must match an allowlist of (method, path) before it is
     sent;
   - every route is GET except POST and DELETE on the watchlist;
   - the server sends the CSRF header itself, because the header guards against
     browsers, not against this process;
   - each route has its own timeout and size cap.
3. **One bearer token per client, with scopes.** Scopes are `read` and `watchlist`
   (`watchlist` implies `read`). The tokens file (`RSBS_MCP_TOKENS_FILE`, default
   `/etc/readsbstats/mcp-tokens`, `root:readsbstats 0640`) stores
   `<name> <scopes> <sha256>`, never a token. The server loads it at startup, refuses a
   group- or world-writable file, and fails closed: with no valid entries every request
   gets 401. Checking is done by a plain ASGI middleware:
   - the presented token is hashed and compared to every entry with
     `hmac.compare_digest`;
   - the matched client is stored in the request scope, and tools read it from there;
   - lifespan scopes pass through untouched.

   `scripts/mcp_token.py add --sha256` registers a token generated on the client host,
   so the secret never reaches the receiver. `new` prints a fresh token only to a
   terminal.
4. **Limits, all charged per tool call.** Each client has a read budget per minute, a
   write budget per hour and a photo budget per hour. A global semaphore with a short
   acquire timeout caps concurrent API calls. Windows for `/api/stats` are snapped (named
   days to local midnight, relative windows floored), so repeated questions hit the
   response cache instead of recomputing under the stats lock. Windows are bounded: at
   most 92 days for stats, or the prewarmed all-time payload; at most 7 days for receiver
   metrics.
5. **Watchlist writes are guarded beyond the API's rules.**
   - Removal is by value, and the server finds the id itself.
   - Adding an existing entry returns that entry.
   - A `callsign_prefix` must be at least 3 characters (the API allows 1, and a short
     prefix would alert on many flights).
   - Additions stop at `RSBS_MCP_WATCHLIST_MAX` entries.
   - Every accepted write is logged with the client name, and the full removed row is
     logged so it can be restored.
6. **Responses are shaped per tool through output allowlists.**
   - The exact receiver location is withheld: no `receiver_*`, no flight bounding boxes,
     no track coordinates in flight profiles, no feeder radar codes or ids.
   - Times become local ISO strings and flag bits become names.
   - Free-text fields from third parties (callsigns, database fields, airport names,
     photographer, labels) are cleaned and length-capped.
   - The server instructions tell the model that values are data, not instructions.
7. **New `origin`, `dest` and `airport` filters on `/api/flights`** and the CSV export.
   They take ICAO airport codes, and an invalid code gets 400. The MCP search tool uses
   them, and the web UI can adopt them later.

## Consequences

- **A second Python process runs on the receiver.** It is idle when nothing calls it.
  Its own systemd limits are `CPUQuota=25%` and `MemoryMax=256M`. `install.sh` installs
  the unit but does not enable it. `update.sh` restarts it only when it is already
  active.
- **New dependency `mcp` (2.3.0).** It brings Starlette extras, pydantic, `pyjwt[crypto]`
  and `httpx2`, which is pinned. Once `httpx2` is installed, Starlette's `TestClient`
  uses it instead of `httpx`.
- **Location: the exact point is hidden, the area is not.** Aircraft positions in live
  and historical snapshots, distances and range-by-bearing are still returned, and from
  them the receiver's area can be estimated.
- **Not every GET is read-only for the database.** An aircraft photo cache miss writes
  the photo cache tables, as it does when the web UI asks for a photo. User data
  changes only through the watchlist tools.
- **Network exposure is the operator's job.** The service binds loopback by default.
  Binding it to a VPN or LAN address needs a firewall that allows the port only from
  the intended clients, plus a matching `RSBS_MCP_ALLOWED_HOSTS` entry: requests with an
  unknown Host header get 421, and any browser Origin gets 403. Over an untrusted LAN, use
  an SSH tunnel or a VPN rather than plain HTTP.
- **Rotation and revocation** edit the tokens file and need
  `systemctl restart readsbstats-mcp`.

## Alternatives considered

- **Give clients the web API through nginx, with a GET-only location, a token and
  limits.** Rejected as the main interface. Raw responses are large and full of
  coordinates, and a model handles dozens of endpoints poorly. The location still
  protects nothing that an in-app allowlist does not.
- **A stdio MCP server on the client host, calling the API over the network.**
  Rejected: it ties the server to one client's setup and still needs the network
  boundary on the receiver.
- **Read SQLite directly in `mode=ro`.** Rejected: it duplicates SQL and the `posenc`
  decoding, bypasses the response caches and the stats lock, and lets one expensive
  query stall the collector's writes.
- **Mount the MCP app inside the web process (in-process ASGI).** Rejected: an MCP
  overload or crash would take the UI down with it, and the database could no longer be
  kept out of reach of the MCP code.
- **Serve the API on a Unix socket for the MCP client.** Rejected for now: it changes
  the web unit, nginx and `update.sh`, and gains little over the loopback client.
- **The SDK's OAuth support.** Rejected: static per-client tokens fit a private
  deployment, and OAuth metadata would advertise an authorization server that does not
  exist.
- **Keep `RSBS_API_TOKEN` as the write guard.** Rejected in 2.30.0: the web UI never
  sent it, so turning it on broke watchlist editing.
