# SQLite WAL mode + per-thread connections in the web server

- Status: ACCEPTED
- Date: 2026-05-09

## Context

The web server uses uvicorn's thread pool to handle requests. SQLite's `sqlite3` module holds a per-connection mutex, so sharing one connection across threads serialises every read. Meanwhile the collector is writing to the database every 5 seconds.

## Decision

Enable WAL (Write-Ahead Logging) mode and open a separate SQLite connection per uvicorn worker thread via `threading.local()` (lazily on first use). This lets readers run concurrently while the collector writes — WAL's design explicitly supports this pattern.

## Consequences

- Read throughput scales with the uvicorn thread pool rather than being serialised.
- Each thread holds its own connection; the connection is reused for the thread's lifetime.
- Tests inject an in-memory DB by setting `web._db` directly — this override is honoured from any thread (in-memory DBs cannot be reopened via a path).
- Never reintroduce a process-wide singleton connection.
