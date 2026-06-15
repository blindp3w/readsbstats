# readsbstats documentation

New here? Start with the [project README](../README.md) — what readsbstats is,
screenshots, and installation. This folder holds the in-depth guides, grouped by
what you're trying to do.

## Reference — look something up

| Guide | Contents |
|---|---|
| [Configuration](configuration.md) | Every `RSBS_*` environment variable (defaults + ranges), logging, airspace, VDL2, database crash-safety |
| [API Reference](api.md) | All HTTP API endpoints, SPA routes, and the SQLite database schema |

## How-to — get a task done

| Guide | Contents |
|---|---|
| [Operations](operations.md) | Updating code, the aircraft/airline DB, backups, retention sizing, VDL2/ACARS ingest, deployment security, crash recovery |
| [Integrations](integrations.md) | Telegram bot setup + commands, ghost-position and ground-speed filtering |
| [Development](development.md) | Local setup, running the collector, tests, building the SPA, deploying to the Pi |
| [dumpvdl2 on an Airspy Mini](dumpvdl2-airspy-mini.md) | Driving dumpvdl2 (ATN CPDLC/ADS-C/MIAM) from an Airspy Mini via external IQ resampling |

## Explanation — understand the why

- [Architecture Decision Records](decisions/README.md) — the rationale behind key technical choices (13 ADRs).

## See also

[Contributing](../CONTRIBUTING.md) · [Security policy](../SECURITY.md) · [Changelog](../CHANGELOG.md) · [Third-party notices](../THIRD_PARTY_NOTICES.md)

---

Guides are organised along [Diátaxis](https://diataxis.fr/) lines — *reference*
(look it up), *how-to* (do a task), *explanation* (understand why).
