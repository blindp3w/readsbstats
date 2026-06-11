# Security Policy

Thanks for taking the time to look at the project's security posture.
**readsbstats** is a solo-maintained hobbyist project. The notes below are
honest about what that means for vulnerability response.

## Threat model

This is important to read first — most of the things that *look* like
vulnerabilities are intentional consequences of the deployment shape:

- The app is designed to run on a **single Raspberry Pi on a trusted home
  LAN**, behind an existing nginx that you control. The web UI is served
  over plain HTTP, has no authentication, and assumes everyone reachable
  on the LAN is trusted.
- The Python services bind to `127.0.0.1` only; nginx is the public-facing
  edge.
- There is no multi-tenant model, no account system, and no public
  internet exposure unless you go out of your way to set one up (which
  this project does not document or support).

A bug is **in scope** when it would expose the Pi, the receiver's data,
or another device on the LAN to an attacker who would otherwise not have
that access — i.e. it weakens what the deployment shape already
guarantees.

## In scope

Please report:

- **Code execution / sandbox escape** (e.g. unsafe `exec`/`eval`/`pickle`,
  shell injection in a `subprocess` call).
- **SQL injection** that bypasses the parameterised-query convention.
- **Server-side request forgery (SSRF)** that escapes the
  `http_safe.safe_urlopen` / `safe_httpx_get` policy (HTTPS-only,
  public-IP-only, no-redirect, size cap).
- **Path traversal** through any env var, query parameter, or
  third-party-sourced data (callsigns, ICAO hex, registrations, type
  descriptions).
- **Reflected or stored XSS** in the web UI (an unsanitised
  `dangerouslySetInnerHTML`, or a third-party URL/href that doesn't go
  through `frontend/src/lib/safeUrl.ts`).
- **Secret leakage**: bot tokens, paths, IPs, or coordinates appearing in
  HTTP responses, logs, or error messages where they shouldn't.
- **Dependency CVEs** in the production dependency set (FastAPI, uvicorn,
  httpx) when there is a realistic exploit path
  reachable from the running services.
- **CSRF or origin-confusion** affecting mutating endpoints (`POST` /
  `DELETE` on `/api/watchlist*`) — the `X-Requested-With` check is the
  only protection and depends on no CORS middleware being added.

## Out of scope

These are known properties of the design and won't be treated as
vulnerabilities. Reports about them will be acknowledged and closed.

- **No authentication on the web UI** — by design, LAN-only.
- **Plain HTTP** — by design; HTTPS is the operator's responsibility if
  they expose the app off-LAN.
- **Telegram bot token visible to local processes on the Pi** — the token
  is in the systemd environment file (`/etc/readsbstats/readsbstats.env`,
  root-owned 0600); a local root attacker already owns the Pi.
- **DoS through high request volume** — nginx `limit_req` is the only
  mitigation; the project doesn't try to be DoS-resilient.
- **Social-engineering or "malicious operator" scenarios** — single-
  operator threat model.
- **Bugs in upstream services** (Planespotters, airport-data.com, hexdb.io,
  adsbdb.com, airplanes.live, Wikipedia, Telegram) — report to them, not
  here. If the consequence is a defence-in-depth gap on our side, that
  *is* in scope (e.g. "if upstream returns a redirect, we follow it
  blindly").
- **Cosmetic CSP relaxations** that don't enable a concrete attack —
  `'unsafe-inline'` is documented as a known trade-off.

## Supported versions

Only the **latest tagged release** and the current `main` branch receive
security fixes. There are no maintenance branches, no LTS, no backports.

| Version           | Supported |
| ----------------- | --------- |
| Latest release    | ✅        |
| `main` (HEAD)     | ✅        |
| Anything older    | ❌        |

Before reporting, please confirm the issue still reproduces on the latest
tag. The current version is in `pyproject.toml`.

## How to report

**Use GitHub's private vulnerability reporting** — it creates a private
discussion with the maintainer, so the details don't sit in public issues
or email logs.

- Open: <https://github.com/blindp3w/readsbstats/security/advisories/new>

If you can't use that for some reason (account restrictions, etc.),
**open a normal GitHub issue with no details**, titled something like
"security report — please request channel" — I'll reply with a way to get
the details to me privately.

**Please don't:**

- Open a public issue or pull request describing the vulnerability before
  it's been fixed.
- Send the details to me via Telegram, public chat, or social channels.

When you do report, include (whatever you have):

- The version or commit hash you reproduced on.
- Steps to reproduce, or a minimal proof-of-concept.
- Your understanding of the impact (what changes hands, what gets
  exposed, what privileges are gained).
- Optional: a suggested patch or test case — really appreciated for
  a solo maintainer.

## What to expect

This is a side project; I have a day job. Best-effort timelines:

- **Acknowledgement** of your report: within ~7 days.
- **Initial triage** (accept / dispute / need-more-info): within ~14 days.
- **Fix landed in `main`** for accepted reports: usually days to a few
  weeks depending on severity. Coordinated disclosure preferred — please
  give me a chance to ship before going public.
- **Tagged release** with the fix: alongside the next release in the
  normal cycle (a separate "security-only" tag if the severity warrants
  it).
- **Public disclosure** in the changelog and a published GitHub Security
  Advisory once the fix is out. Reporters are credited by name or handle
  if they want — or kept anonymous if they prefer.

If you get no response in ~14 days, please nudge me (a follow-up in the
same private advisory is fine). No bug bounty, no swag — just thanks,
credit, and a fixed bug.

## Past security work

The project keeps a running internal audit tracker. Every prior
externally-relevant security item has shipped publicly through the normal
changelog flow — see entries tagged "security" in
[CHANGELOG.md](CHANGELOG.md), especially v1.1.1, v1.5.1, v1.6.0, and
v1.8.1.
