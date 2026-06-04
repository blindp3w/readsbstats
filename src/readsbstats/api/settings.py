"""Runtime settings snapshot.

Per-domain helpers (``_settings_receiver``/``_collector``/``_database``/
``_enrichment``/``_metrics``/``_health``/``_ui``/``_telegram``) shape the
payload alongside the settings-page-internal masking rules; the public
``_settings_payload`` glues them together and ``_settings_metadata``
adds the ``_metadata`` block describing each key's env var, default,
and customised status.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from .. import config


router = APIRouter()


def _settings_receiver() -> dict:
    return {
        "lat":       config.RECEIVER_LAT,
        "lon":       config.RECEIVER_LON,
        "max_range": config.RECEIVER_MAX_RANGE,
    }


def _settings_collector() -> dict:
    return {
        "poll_interval": config.POLL_INTERVAL_SEC,
        "flight_gap":    config.FLIGHT_GAP_SEC,
        "min_positions": config.MIN_POSITIONS_KEEP,
        "max_seen_pos":  config.MAX_SEEN_POS_SEC,
        "max_speed_kts": config.MAX_SPEED_KTS,
    }


def _settings_database() -> dict:
    # Mask filesystem paths — operator just needs to know whether a custom
    # value was set, not the actual path on disk (#H8 + audit-12 #171).
    return {
        "db_path":        os.path.basename(config.DB_PATH) or "(default)",
        "retention_days": config.RETENTION_DAYS,
        "purge_interval": config.PURGE_INTERVAL_SEC,
    }


def _settings_enrichment() -> dict:
    airspace_label = "(set)" if config.AIRSPACE_GEOJSON else "(bundled poland.geojson)"
    return {
        "photo_cache_days": config.PHOTO_CACHE_DAYS,
        "airspace_geojson": airspace_label,
        "route_cache_days": config.ROUTE_CACHE_DAYS,
        "route_interval":   config.ROUTE_ENRICH_INTERVAL,
        "route_batch":      config.ROUTE_BATCH_SIZE,
        "route_rate_limit": config.ROUTE_RATE_LIMIT_SEC,
        # External ADS-B enrichment
        "adsbx_enabled":  config.ADSBX_ENABLED,
        "adsbx_interval": config.ADSBX_POLL_INTERVAL,
        "adsbx_range":    config.ADSBX_RANGE_NM,
        "adsbx_url":      config.ADSBX_API_URL,
    }


def _settings_metrics() -> dict:
    # Audit-12 P8: stats_json previously compared against a literal
    # `/run/readsb/stats.json`. Just report "(configured)" for any non-empty
    # value — avoids duplicating the default and avoids leaking the one bit
    # of "was it customised".
    return {
        "metrics_enabled":  config.METRICS_ENABLED,
        "metrics_interval": config.METRICS_INTERVAL,
        "stats_json":       "(configured)" if config.STATS_JSON else "(not set)",
    }


def _settings_health() -> dict:
    return {
        "health_heartbeat_warn_s":     config.HEALTH_HEARTBEAT_WARN_S,
        "health_heartbeat_crit_s":     config.HEALTH_HEARTBEAT_CRIT_S,
        "health_aircraft_gap_s":       config.HEALTH_AIRCRAFT_GAP_S,
        "health_noise_warn_db":        config.HEALTH_NOISE_WARN_DB,
        "health_noise_crit_db":        config.HEALTH_NOISE_CRIT_DB,
        "health_cpu_warn_pct":         config.HEALTH_CPU_WARN_PCT,
        "health_cpu_crit_pct":         config.HEALTH_CPU_CRIT_PCT,
        "health_baseline_weeks":       config.HEALTH_BASELINE_WEEKS,
        "health_baseline_min_samples": config.HEALTH_BASELINE_MIN_SAMPLES,
        "health_msg_drop_pct":         config.HEALTH_MSG_DROP_PCT,
        "health_aircraft_drop_pct":    config.HEALTH_AIRCRAFT_DROP_PCT,
        "health_signal_drop_db":       config.HEALTH_SIGNAL_DROP_DB,
        "health_gain_strong_pct":      config.HEALTH_GAIN_STRONG_PCT,
        "health_range_short_days":     config.HEALTH_RANGE_SHORT_DAYS,
        "health_range_long_days":      config.HEALTH_RANGE_LONG_DAYS,
        "health_range_ratio":          config.HEALTH_RANGE_RATIO,
    }


def _settings_ui() -> dict:
    # web_host / web_port intentionally omitted (audit-12 #171): the client
    # is already at that URL, and on a reverse-proxied deploy the bind host
    # (0.0.0.0) would be misleading anyway.
    return {
        "root_path":         config.ROOT_PATH,
        "page_size":         config.DEFAULT_PAGE_SIZE,
        "max_page_size":     config.MAX_PAGE_SIZE,
        "time_format":       config.TIME_FORMAT,
        "map_history_hours": config.MAP_HISTORY_HOURS,
    }


def _settings_vdl2() -> dict:
    # Opt-in VDL2/ACARS feature. `vdl2_enabled` is the capability flag the SPA
    # reads to show/hide the Messages tab. Mask the DB path (secret), like db_path.
    return {
        "vdl2_enabled":   config.VDL2_ENABLED,
        "vdl2_db_path":   os.path.basename(config.VDL2_DB_PATH) or "(default)",
        "vdl2_retention": config.VDL2_RETENTION_DAYS,
    }


def _settings_telegram() -> dict:
    # Mask the token + chat id (#H8 + audit-12 #171) — consumer never sees
    # raw secrets, just whether they're configured.
    return {
        "telegram_token":        "configured" if config.TELEGRAM_TOKEN else "not set",
        "telegram_chat_id":      "configured" if config.TELEGRAM_CHAT_ID else "not set",
        "telegram_summary_time": config.TELEGRAM_SUMMARY_TIME,
        "telegram_units":        config.TELEGRAM_UNITS,
        "base_url":              config.TELEGRAM_BASE_URL,
    }


def _settings_payload() -> dict:
    """Return the runtime-settings dict shown on the React /v2/settings page.

    improvements.md A13-083: decomposed into per-domain helpers so each group
    of settings lives next to its own masking / formatting rules.  The
    payload shape (one flat dict of every key) is unchanged.
    """
    return {
        **_settings_receiver(),
        **_settings_collector(),
        **_settings_database(),
        **_settings_enrichment(),
        **_settings_metrics(),
        **_settings_health(),
        **_settings_ui(),
        **_settings_telegram(),
        **_settings_vdl2(),
    }


# Falsy bool strings — kept in sync with `config._BOOL_FALSY`. Duplicated
# rather than imported to avoid widening config.py's public API surface.
_SETTINGS_BOOL_FALSY = frozenset({"", "0", "false", "no", "off"})


def _settings_default_as_parsed(default, current):
    """Cast `default` (as recorded in `_META_REGISTRY`) to the type of
    `current` (the parsed config attribute). Mirrors what the
    `_int/_float/_bool/os.getenv` parsers do at module load."""
    if isinstance(current, bool):
        if isinstance(default, bool):
            return default
        return str(default).strip().lower() not in _SETTINGS_BOOL_FALSY
    if isinstance(current, int):
        return int(default)
    if isinstance(current, float):
        return float(default)
    return str(default) if default is not None else ""


def _settings_metadata(config_namespace, payload_keys) -> dict:
    """Build the `_metadata` block keyed by payload_key. Pure function so
    tests can pass a `SimpleNamespace` stub.

    For each payload key it emits ``{env_var, default, customized}``.
    `customized` compares the **raw** config attribute against the
    registered default, not the masked display value — otherwise
    `telegram_token` would always read as customized regardless of
    whether the operator actually set it.
    """
    registry = getattr(config, "_META_REGISTRY", {})
    out: dict[str, dict] = {}
    for key in payload_keys:
        reg = registry.get(key)
        if reg is None:
            continue
        attr = reg["config_attr"]
        raw_value = getattr(config_namespace, attr, None)
        parsed_default = _settings_default_as_parsed(reg["default"], raw_value)
        customized = raw_value != parsed_default
        # Float tolerance: parsed defaults can re-roundtrip to a slightly
        # different float (e.g. "-25" → -25.0). Treat exact-equal-after-
        # round as not-customized.
        if isinstance(raw_value, float) and isinstance(parsed_default, float):
            customized = abs(raw_value - parsed_default) > 1e-9
        # Filesystem-path / secret defaults are masked: shipping the raw
        # default would leak install paths through /api/settings. The
        # `customized` flag is still meaningful, and the displayed payload
        # value is already masked by the corresponding `_settings_*` helper.
        if reg.get("secret"):
            out_default = None
        else:
            out_default = parsed_default
        out[key] = {
            "env_var":    reg["env_var"],
            "default":    out_default,
            "customized": bool(customized),
        }
    return out


@router.get("/api/settings")
def api_settings() -> dict:
    payload = _settings_payload()
    payload["_metadata"] = _settings_metadata(
        config, [k for k in payload if k != "_metadata"]
    )
    return payload
