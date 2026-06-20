"""
readsbstats — receiver health dashboard.

Runs rule-based checks over the ``receiver_stats`` time-series and returns an
overall status plus per-check details.  Phase 1 ships hard rules only:
heartbeat, aircraft visibility, noise floor, demod CPU saturation.

Baseline-aware checks, gain hints, and Telegram alerting land in later phases.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass

from . import config

_log = logging.getLogger(__name__)


# Severity ordering — higher number is worse, used to pick overall status.
# `info` ranks BELOW `ok` so that baseline-warm-up checks don't drag the overall
# state down: a fresh install showing Phase 1 ok + Phase 2 info reads as "ok".
_SEVERITY_RANK = {"info": 0, "ok": 1, "warn": 2, "critical": 3}

# _baseline_avg / _recent_avg interpolate `column` into SQL via f-string —
# safe today because all call sites pass literals, but a single mistake (or
# a future caller forwarding a query param) becomes a SQL-injection sink.
# Defence-in-depth: explicit allowlist of every column legitimately used as
# a baseline metric.
_BASELINE_ALLOWED_COLS = frozenset({"messages", "signal", "ac_with_pos"})

# readsb's demod-CPU figure is milliseconds per `last1min` stats window, which
# upstream aggregates over a fixed 60-second window (independent of the
# collector poll cadence). Used by `_check_cpu_saturation` to convert ms → % of
# one core. Hoisted to module scope (STY-3) — it was a per-call function local.
_READSB_STATS_WINDOW_MS = 60_000


@dataclass
class Check:
    name: str
    severity: str
    message: str
    value: float | int | None = None
    threshold: float | int | None = None


@dataclass
class HealthReport:
    overall: str
    as_of: int
    checks: list[Check]

    def to_dict(self) -> dict:
        return {
            "overall": self.overall,
            "as_of": self.as_of,
            "checks": [asdict(c) for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Individual checks — each returns a single Check
# ---------------------------------------------------------------------------

def _check_heartbeat(conn: sqlite3.Connection, now: int) -> Check:
    row = conn.execute("SELECT MAX(ts) AS ts FROM receiver_stats").fetchone()
    last_ts = row["ts"] if row and row["ts"] is not None else None
    if last_ts is None:
        # Audit-13 A13-025: a fresh install with metrics disabled is not a
        # failure mode — the absence of receiver_stats rows is the operator's
        # decision (RSBS_METRICS_ENABLED=0). `info` reads as "everything is
        # fine, here's a note" on the stripe; `warn` was over-claiming.
        return Check(
            name="heartbeat",
            severity="info",
            message="No receiver metrics recorded yet — set RSBS_METRICS_ENABLED=1 in the collector",
        )
    age = now - last_ts
    if age >= config.HEALTH_HEARTBEAT_CRIT_S:
        return Check(
            name="heartbeat",
            severity="critical",
            message=f"No metrics update for {age}s — collector or readsb may be down",
            value=age,
            threshold=config.HEALTH_HEARTBEAT_CRIT_S,
        )
    if age >= config.HEALTH_HEARTBEAT_WARN_S:
        return Check(
            name="heartbeat",
            severity="warn",
            message=f"Last metrics update {age}s ago",
            value=age,
            threshold=config.HEALTH_HEARTBEAT_WARN_S,
        )
    return Check(
        name="heartbeat",
        severity="ok",
        message=f"Metrics fresh ({age}s old)",
        value=age,
    )


def _check_aircraft_visibility(conn: sqlite3.Connection, now: int) -> Check:
    window = config.HEALTH_AIRCRAFT_GAP_S
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(ac_with_pos), 0) AS peak "
        "FROM receiver_stats WHERE ts >= ?",
        (now - window,),
    ).fetchone()
    if not row or row["n"] == 0:
        return Check(
            name="aircraft_visibility",
            severity="info",
            message="No metrics rows in recent window",
        )
    if row["peak"] == 0:
        return Check(
            name="aircraft_visibility",
            severity="critical",
            message=f"No aircraft with position seen in last {window}s — antenna or RF chain may be down",
            value=0,
            threshold=1,
        )
    return Check(
        name="aircraft_visibility",
        severity="ok",
        message=f"Peak {row['peak']} aircraft with position in last {window}s",
        value=row["peak"],
    )


def _check_noise_floor(conn: sqlite3.Connection, now: int) -> Check:
    window = 600  # 10-minute average smooths instantaneous spikes
    row = conn.execute(
        "SELECT AVG(noise) AS avg_noise FROM receiver_stats WHERE ts >= ?",
        (now - window,),
    ).fetchone()
    avg = row["avg_noise"] if row else None
    if avg is None:
        return Check(
            name="noise_floor",
            severity="info",
            message="No noise measurements in recent window",
        )
    avg_r = round(avg, 1)
    if avg >= config.HEALTH_NOISE_CRIT_DB:
        return Check(
            name="noise_floor",
            severity="critical",
            message=f"Noise floor {avg_r} dBFS — possible RF interference or DC spike",
            value=avg_r,
            threshold=config.HEALTH_NOISE_CRIT_DB,
        )
    if avg >= config.HEALTH_NOISE_WARN_DB:
        return Check(
            name="noise_floor",
            severity="warn",
            message=f"Noise floor {avg_r} dBFS — higher than ideal",
            value=avg_r,
            threshold=config.HEALTH_NOISE_WARN_DB,
        )
    return Check(
        name="noise_floor",
        severity="ok",
        message=f"Noise floor {avg_r} dBFS",
        value=avg_r,
    )


def _check_cpu_saturation(conn: sqlite3.Connection, now: int) -> Check:
    """Demod CPU is in ms-per-stats-window; convert to % of one core.

    readsb's `last1min` block aggregates over a fixed 60-second window
    upstream — independent of how often the collector polls. Audit-13
    (A13-007) previously divided by `config.METRICS_INTERVAL * 1000`,
    which double-counted the cadence: setting `RSBS_METRICS_INTERVAL=30`
    silently doubled the reported demod-%. Use the literal upstream
    window length here.
    """
    window = 300
    row = conn.execute(
        "SELECT AVG(cpu_demod) AS avg_cpu FROM receiver_stats WHERE ts >= ?",
        (now - window,),
    ).fetchone()
    avg_ms = row["avg_cpu"] if row else None
    if avg_ms is None:
        return Check(
            name="cpu_saturation",
            severity="info",
            message="No CPU measurements in recent window",
        )
    pct = (avg_ms / _READSB_STATS_WINDOW_MS) * 100
    pct_r = round(pct, 1)
    if pct >= config.HEALTH_CPU_CRIT_PCT:
        return Check(
            name="cpu_saturation",
            severity="critical",
            message=f"Demodulator CPU at {pct_r}% — decode quality likely degraded",
            value=pct_r,
            threshold=config.HEALTH_CPU_CRIT_PCT,
        )
    if pct >= config.HEALTH_CPU_WARN_PCT:
        return Check(
            name="cpu_saturation",
            severity="warn",
            message=f"Demodulator CPU at {pct_r}%",
            value=pct_r,
            threshold=config.HEALTH_CPU_WARN_PCT,
        )
    return Check(
        name="cpu_saturation",
        severity="ok",
        message=f"Demodulator CPU at {pct_r}%",
        value=pct_r,
    )


# ---------------------------------------------------------------------------
# Phase 2 — baseline-aware checks (same hour-of-week, prior weeks)
# ---------------------------------------------------------------------------

def _baseline_avg(
    conn: sqlite3.Connection,
    column: str,
    now: int,
    *,
    lookback_weeks: int,
) -> tuple[float | None, int]:
    """
    Return (average, sample_count) for `column` over rows that share the same
    local DOW+hour as `now`, sampled across the past `lookback_weeks` weeks
    (excluding the current hour).  None if no samples found.

    improvements.md A13-057: builds an OR-of-narrow-BETWEEN clause from
    per-week target windows computed in Python.  Each week's window is
    one local hour wide (the hour containing the target wall-clock time),
    so the planner can use ``idx_receiver_stats_ts`` instead of scanning
    the full lookback range and filtering with strftime per row.  DST is
    handled correctly because Python's naive ``datetime`` + ``timedelta``
    arithmetic on local time, followed by ``.timestamp()``, accounts for
    the offset in effect at the historical wall-clock time.
    """
    if column not in _BASELINE_ALLOWED_COLS:
        raise ValueError(f"column {column!r} not allowed for baseline avg")
    if lookback_weeks < 1:
        return None, 0

    target = _dt.datetime.fromtimestamp(now).replace(
        minute=0, second=0, microsecond=0
    )
    # One narrow window per week back.  Same DOW+hour is guaranteed because
    # we subtract whole weeks of local wall-clock time, so the strftime
    # filter from the old implementation is no longer needed.
    windows: list[tuple[int, int]] = []
    for w in range(1, lookback_weeks + 1):
        start_dt = target - _dt.timedelta(weeks=w)
        end_dt = start_dt + _dt.timedelta(hours=1)
        windows.append((int(start_dt.timestamp()), int(end_dt.timestamp())))

    clause = " OR ".join("(ts BETWEEN ? AND ?)" for _ in windows)
    params: list = [v for win in windows for v in win]
    row = conn.execute(
        f"""
        SELECT AVG({column}) AS avg, COUNT({column}) AS n
        FROM receiver_stats
        WHERE ({clause})
          AND {column} IS NOT NULL
        """,
        params,
    ).fetchone()
    if not row or row["n"] == 0:
        return None, 0
    return row["avg"], row["n"]


def _recent_avg(
    conn: sqlite3.Connection,
    column: str,
    now: int,
    *,
    window_s: int,
) -> tuple[float | None, int]:
    """Return (average, sample_count) for `column` over the last `window_s` seconds."""
    if column not in _BASELINE_ALLOWED_COLS:
        raise ValueError(f"column {column!r} not allowed for recent avg")
    row = conn.execute(
        f"SELECT AVG({column}) AS avg, COUNT({column}) AS n "
        f"FROM receiver_stats WHERE ts >= ?",
        (now - window_s,),
    ).fetchone()
    if not row or row["n"] == 0:
        return None, 0
    return row["avg"], row["n"]


def _insufficient_baseline(name: str, n: int) -> Check:
    return Check(
        name=name,
        severity="info",
        message=f"Baseline still warming up ({n} samples; need {config.HEALTH_BASELINE_MIN_SAMPLES})",
    )


def _check_message_rate(conn: sqlite3.Connection, now: int) -> Check:
    """Warn if recent msg/min average drops below `HEALTH_MSG_DROP_PCT` of historical baseline."""
    current, _ = _recent_avg(conn, "messages", now, window_s=900)
    if current is None:
        return Check(name="message_rate", severity="info", message="No recent message rate samples")
    baseline, n = _baseline_avg(conn, "messages", now, lookback_weeks=config.HEALTH_BASELINE_WEEKS)
    if baseline is None or n < config.HEALTH_BASELINE_MIN_SAMPLES:
        return _insufficient_baseline("message_rate", n)
    if baseline <= 0:
        return Check(name="message_rate", severity="info", message="Baseline is zero — receiver historically idle")
    pct = (current / baseline) * 100
    threshold = config.HEALTH_MSG_DROP_PCT
    msg = f"{int(current)}/min vs {int(baseline)}/min baseline ({pct:.0f}%)"
    if pct < threshold:
        return Check(
            name="message_rate",
            severity="warn",
            message=f"Message rate {msg} — well below normal for this hour",
            value=round(pct, 1),
            threshold=threshold,
        )
    return Check(name="message_rate", severity="ok", message=msg, value=round(pct, 1))


def _check_signal_drop(conn: sqlite3.Connection, now: int) -> Check:
    """Warn if recent average signal is more than `HEALTH_SIGNAL_DROP_DB` below baseline."""
    current, _ = _recent_avg(conn, "signal", now, window_s=600)
    if current is None:
        return Check(name="signal_drop", severity="info", message="No recent signal samples")
    baseline, n = _baseline_avg(conn, "signal", now, lookback_weeks=config.HEALTH_BASELINE_WEEKS)
    if baseline is None or n < config.HEALTH_BASELINE_MIN_SAMPLES:
        return _insufficient_baseline("signal_drop", n)
    # Audit-13 A13-023: defensive guard mirroring `_check_message_rate`'s
    # `baseline <= 0` short-circuit. Signal in dBFS is normally negative
    # (~−40 strong, ~−50 moderate); a baseline of exactly 0 dBFS is
    # physically improbable and most likely indicates degenerate
    # historical data. Without the guard, current=−42 vs baseline=0
    # produces delta=−42, which slams every threshold and surfaces a
    # spurious "antenna degraded" warn.
    if baseline >= 0:
        return Check(name="signal_drop", severity="info", message="Baseline ≥ 0 dBFS — degenerate history or saturated front-end; signal-drop check skipped")
    delta = current - baseline
    threshold = -config.HEALTH_SIGNAL_DROP_DB
    msg = f"{current:.1f} dBFS vs {baseline:.1f} dBFS baseline (Δ {delta:+.1f} dB)"
    if delta < threshold:
        return Check(
            name="signal_drop",
            severity="warn",
            message=f"Signal {msg} — antenna or RF chain may be degraded",
            value=round(delta, 1),
            threshold=threshold,
        )
    return Check(name="signal_drop", severity="ok", message=msg, value=round(delta, 1))


def _check_aircraft_drop(conn: sqlite3.Connection, now: int) -> Check:
    """Warn if recent aircraft count drops below `HEALTH_AIRCRAFT_DROP_PCT` of baseline."""
    current, _ = _recent_avg(conn, "ac_with_pos", now, window_s=600)
    if current is None:
        return Check(name="aircraft_drop", severity="info", message="No recent aircraft samples")
    baseline, n = _baseline_avg(conn, "ac_with_pos", now, lookback_weeks=config.HEALTH_BASELINE_WEEKS)
    if baseline is None or n < config.HEALTH_BASELINE_MIN_SAMPLES:
        return _insufficient_baseline("aircraft_drop", n)
    if baseline < 1:
        # Quiet hour historically — relative drops are noisy, skip the check
        return Check(name="aircraft_drop", severity="ok", message="Quiet hour (low historical baseline)")
    pct = (current / baseline) * 100
    threshold = config.HEALTH_AIRCRAFT_DROP_PCT
    msg = f"{current:.1f} avg vs {baseline:.1f} baseline ({pct:.0f}%)"
    if pct < threshold:
        return Check(
            name="aircraft_drop",
            severity="warn",
            message=f"Aircraft count {msg} — abnormally low for this hour of week",
            value=round(pct, 1),
            threshold=threshold,
        )
    return Check(name="aircraft_drop", severity="ok", message=msg, value=round(pct, 1))


# ---------------------------------------------------------------------------
# Phase 3 — gain hints (productizes the manual gain-tuning workflow)
# ---------------------------------------------------------------------------

def _check_gain_saturation(conn: sqlite3.Connection, now: int) -> Check:
    """
    Warn if the strong-signal ratio (signals > -3 dBFS) exceeds the threshold —
    a known indicator that gain is set too high, causing decode errors and MLAT
    sync issues.  See `internal_docs/internal/gain-tuning-log.md` for context.
    """
    window = 600
    row = conn.execute(
        "SELECT SUM(strong_signals) AS strong, SUM(messages) AS msg "
        "FROM receiver_stats WHERE ts >= ?",
        (now - window,),
    ).fetchone()
    if not row or not row["msg"]:
        return Check(name="gain_saturation", severity="info", message="No recent message data")
    msg_total = row["msg"]
    strong_total = row["strong"] or 0
    pct = (strong_total / msg_total) * 100
    pct_r = round(pct, 2)
    threshold = config.HEALTH_GAIN_STRONG_PCT
    summary = f"Strong signals at {pct_r}% of messages"
    if pct > threshold:
        return Check(
            name="gain_saturation",
            severity="warn",
            message=f"{summary} — gain may be too high; consider lowering by 1–2 dB",
            value=pct_r,
            threshold=threshold,
        )
    return Check(name="gain_saturation", severity="ok", message=summary, value=pct_r)


def _check_range_degradation(conn: sqlite3.Connection, now: int) -> Check:
    """
    Slow drift signal: short-window max range divided by long-window max range.
    If the recent 7-day peak is materially below the 30-day peak it's likely
    antenna, connector, or gain drift.  Severity stays at `info` — atmospheric
    variability makes this noisy, so the goal is gentle awareness, not alarm.
    """
    short_s = config.HEALTH_RANGE_SHORT_DAYS * 86400
    long_s  = config.HEALTH_RANGE_LONG_DAYS * 86400
    # Audit-13 A13-011: combined into one query; guards `long_max <= 0` to
    # avoid div-by-zero if receiver_stats contains a zero/NULL row.
    row = conn.execute(
        """
        SELECT MAX(CASE WHEN ts >= ? THEN max_distance_m END) AS short_max,
               MAX(max_distance_m) AS long_max,
               MIN(ts) AS first
        FROM receiver_stats
        WHERE ts >= ?
        """,
        (now - short_s, now - long_s),
    ).fetchone()
    short_max = row["short_max"] if row else None
    long_max = row["long_max"] if row else None
    first_ts = row["first"] if row else None

    if not short_max or not long_max or long_max <= 0:
        return Check(name="range_degradation", severity="info", message="No range data yet")

    # Need at least 2× the short window of total history before the comparison
    # is meaningful — otherwise we're comparing 7d to 7d.
    history_s = now - first_ts if first_ts else 0
    if history_s < 2 * short_s:
        return Check(
            name="range_degradation",
            severity="info",
            message=f"Collecting range history ({history_s // 86400}d so far)",
        )

    ratio = short_max / long_max
    short_nm = short_max / 1852
    long_nm  = long_max / 1852
    summary = (
        f"{config.HEALTH_RANGE_SHORT_DAYS}d max {short_nm:.0f} nm "
        f"vs {config.HEALTH_RANGE_LONG_DAYS}d max {long_nm:.0f} nm "
        f"({ratio * 100:.0f}%)"
    )
    if ratio < config.HEALTH_RANGE_RATIO:
        return Check(
            name="range_degradation",
            severity="info",
            message=f"{summary} — possible antenna or connector degradation",
            value=round(ratio * 100, 1),
            threshold=round(config.HEALTH_RANGE_RATIO * 100, 1),
        )
    return Check(name="range_degradation", severity="ok", message=summary, value=round(ratio * 100, 1))


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

_CHECKS = (
    _check_heartbeat,
    _check_aircraft_visibility,
    _check_noise_floor,
    _check_cpu_saturation,
    _check_message_rate,
    _check_signal_drop,
    _check_aircraft_drop,
    _check_gain_saturation,
    _check_range_degradation,
)


def compute_health(conn: sqlite3.Connection, now: int | None = None) -> HealthReport:
    """Run all enabled checks and return a HealthReport.

    Audit-13 A13-008: each check is isolated in its own try/except so a
    single failing check (DB hiccup, unexpected row shape) cannot bring
    down the entire dashboard. Failures degrade to ``severity="info"``
    rather than 500.
    """
    if now is None:
        now = int(time.time())
    checks: list[Check] = []
    for fn in _CHECKS:
        try:
            checks.append(fn(conn, now))
        except Exception:  # noqa: BLE001 - per-check isolation, must catch all
            _log.exception("health check %s raised", fn.__name__)
            checks.append(Check(
                name=fn.__name__.lstrip("_").removeprefix("check_"),
                severity="info",
                message="check failed (see server logs)",
            ))
    overall = max(checks, key=lambda c: _SEVERITY_RANK.get(c.severity, 0)).severity
    return HealthReport(overall=overall, as_of=now, checks=checks)
