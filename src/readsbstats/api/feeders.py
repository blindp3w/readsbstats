"""ADS-B feeder health endpoint and helpers.

Each /api/feeders request fans out a systemctl is-active + TCP probe + a
detail fetcher per configured feeder (readsb / fr24 / piaware / mlat).
Cached with a short TTL (10 s) and coalesced via ``cache._feeder_lock``
so concurrent requests don't multiply the subprocess load (BE-18).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter

from .. import cache, config


log = logging.getLogger("web")
router = APIRouter()


async def _check_systemd_unit(unit: str) -> dict:
    """Run ``systemctl is-active <unit>`` and return the status string.

    Audit-13 A13-042: reject unit names that start with ``-`` (would be
    interpreted as systemctl flags) and pass ``--`` between args and the
    unit so even a name containing ``--foo`` is treated as positional.
    """
    if unit.startswith("-"):
        return {"systemd": "invalid-unit-name"}
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", "--", unit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        return {"systemd": stdout.decode().strip() or "unknown"}
    except FileNotFoundError:
        return {"systemd": "unavailable"}
    except asyncio.TimeoutError:
        # Don't leak the child process — kill it and reap (audit-12 #152).
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return {"systemd": "timeout"}
    except Exception:
        # STY-5: don't echo the raw exception into the status payload (info
        # leak + noisy UI). Return a fixed token and log the detail server-side.
        log.warning("systemd unit check failed", exc_info=True)
        return {"systemd": "error"}


async def _check_port(port: int, host: str = "127.0.0.1") -> dict:
    """Try to open a TCP connection to *host*:*port*."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3.0,
        )
        writer.close()
        await writer.wait_closed()
        return {"port": port, "port_status": "open"}
    except (ConnectionRefusedError, OSError):
        return {"port": port, "port_status": "closed"}
    except asyncio.TimeoutError:
        return {"port": port, "port_status": "timeout"}


def _read_json_file(path: str) -> dict | None:
    """Read and parse a JSON file, returning None on any error.

    Missing files are a normal "feeder not configured" signal — silent.
    Malformed JSON / OS errors are logged at debug level so operators
    can correlate a missing feeder card with the underlying cause.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("feeder status %s unreadable: %s", path, type(exc).__name__)
        return None


def _feeder_details_readsb(status_path: str) -> list[tuple[str, str]]:
    """Extract key stats from a readsb JSON directory."""
    details: list[tuple[str, str]] = []
    ac = _read_json_file(f"{status_path}/aircraft.json")
    if ac:
        count = len(ac.get("aircraft", []))
        details.append(("Aircraft tracked", str(count)))
    stats = _read_json_file(f"{status_path}/stats.json")
    if stats:
        last = stats.get("last1min", {})
        msgs = last.get("messages", 0)
        if msgs:
            start = last.get("start", 0)
            end = last.get("end", 0)
            # STY-6: only emit a rate when the window duration is actually known
            # (end > start). Missing/zero start/end means we can't compute msgs/s
            # — omit the row rather than divide by a stale 60 s and show a wrong
            # number.
            if end > start:
                details.append(("Messages/s", f"{msgs / (end - start):.0f}"))
        local = last.get("local", {})
        if "signal" in local:
            details.append(("Signal", f"{local['signal']:.1f} dBFS"))
        if "noise" in local:
            details.append(("Noise", f"{local['noise']:.1f} dBFS"))
        max_dist = last.get("max_distance")
        if max_dist:
            details.append(("Max range", f"{max_dist / 1852:.1f}"))
    return details


# W-2 (Audit 2026-06-01): loopback-only carve-out from http_safe.safe_httpx_get
# (which is HTTPS-only). The URL is pre-validated to loopback by
# _is_safe_status_url before this function is called. The 256 KB cap protects
# against a misbehaving local FR24 daemon — monitor.json is ~10 KB in practice.
# A naive post-`get()` len(resp.content) check is insufficient: httpx buffers
# the full body before returning. Streaming via aiter_bytes() lets us abort
# mid-stream, mirroring http_safe.safe_httpx_get's pattern.
_FR24_MAX_BYTES = 256 * 1024


async def _feeder_details_fr24(status_url: str) -> list[tuple[str, str]]:
    """Fetch FR24 monitor.json and extract key fields."""
    details: list[tuple[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=False) as client:
            async with client.stream("GET", status_url) as resp:
                resp.raise_for_status()
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _FR24_MAX_BYTES:
                        # Carve-out cap: abort mid-stream, don't buffer further.
                        log.debug(
                            "FR24 monitor.json exceeded %d KB cap; discarding",
                            _FR24_MAX_BYTES // 1024,
                        )
                        return details
                data = json.loads(bytes(buf))
    except Exception:
        return details
    if data.get("build_version"):
        details.append(("Version", data["build_version"]))
    fs = data.get("feed_status")
    if fs:
        details.append(("FR24 link", fs))
    alias = data.get("feed_alias")
    if alias:
        details.append(("Radar code", alias))
    ac = data.get("feed_num_ac_tracked")
    if ac is not None:
        details.append(("Aircraft tracked", str(ac)))
    rx = data.get("rx_connected")
    if rx is not None:
        details.append(("Receiver", "connected" if str(rx) == "1" else "disconnected"))
    mlat_ok = data.get("mlat-ok")
    if mlat_ok is not None:
        details.append(("MLAT", "ok" if str(mlat_ok) == "1" else "not ok"))
    return details


def _feeder_details_piaware(status_path: str) -> list[tuple[str, str]]:
    """Read PiAware status.json and extract component statuses."""
    details: list[tuple[str, str]] = []
    data = _read_json_file(status_path)
    if not data:
        return details
    ver = data.get("piaware_version")
    if ver:
        details.append(("Version", f"PiAware {ver}"))
    for key in ("piaware", "adept", "radio", "mlat"):
        comp = data.get(key)
        if comp and isinstance(comp, dict):
            msg = comp.get("message", comp.get("status", ""))
            if msg:
                details.append((key.capitalize(), msg))
    cpu = data.get("cpu_temp_celcius")
    if cpu is not None:
        details.append(("CPU temp", f"{cpu:.0f} C"))
    return details


async def _feeder_details_mlat(unit: str) -> list[tuple[str, str]]:
    """Parse recent journald output for mlat-client stats.

    Audit-13 A13-042: reject unit names that start with ``-`` (would be
    misread as a journalctl flag) before invoking the subprocess.
    """
    details: list[tuple[str, str]] = []
    if unit.startswith("-"):
        return details
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", unit, "--no-pager", "-n", "30", "-o", "cat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        lines = stdout.decode(errors="replace").splitlines()
    except asyncio.TimeoutError:
        # Don't leak the child — kill + reap (audit-12 #152).
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return details
    except Exception:
        return details
    for line in reversed(lines):
        if not details or len(details) < 4:
            m = re.search(r"Results:\s+([\d.]+)\s+positions/minute", line)
            if m and not any(k == "Positions/min" for k, _ in details):
                details.append(("Positions/min", m.group(1)))
            m = re.search(r"Aircraft:\s+(.+)", line)
            if m and not any(k == "Aircraft" for k, _ in details):
                details.append(("Aircraft", m.group(1)))
            m = re.search(r"peer_count:\s+(\d+)", line)
            if m and not any(k == "Peers" for k, _ in details):
                details.append(("Peers", m.group(1)))
            m = re.search(r"Server:\s+(\S+)", line)
            if m and not any(k == "Server" for k, _ in details):
                details.append(("Server", m.group(1)))
    return details


_FEEDER_STATUS_URL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_safe_status_path(path: str) -> bool:
    """A feeder status_path comes from RSBS_FEEDERS (env-controlled). Only allow
    paths that resolve under ``config.FEEDER_STATUS_ROOT`` (default ``/run``)
    — defence-in-depth against path traversal if the env is ever attacker-
    controlled.  The root is read at call time so tests can monkeypatch.
    """
    if not isinstance(path, str) or not path:
        return False
    try:
        resolved = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    root = config.FEEDER_STATUS_ROOT
    return resolved == root or resolved.startswith(root + "/")


def _is_safe_status_url(url: str) -> bool:
    """A feeder status_url comes from RSBS_FEEDERS (env-controlled). Only allow
    plain http on a loopback host — defence-in-depth against SSRF if the env
    is ever attacker-controlled."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "http" and parsed.hostname in _FEEDER_STATUS_URL_HOSTS


async def _fetch_feeder_details(feeder: dict) -> list[tuple[str, str]]:
    """Dispatch to the appropriate detail fetcher based on status_type."""
    st = feeder.get("status_type")
    try:
        if st == "readsb" and feeder.get("status_path"):
            if not _is_safe_status_path(feeder["status_path"]):
                log.warning("feeder %r: rejecting status_path %r (must be under %s/)",
                            feeder.get("name"), feeder["status_path"], config.FEEDER_STATUS_ROOT)
                return []
            return _feeder_details_readsb(feeder["status_path"])
        if st == "fr24" and feeder.get("status_url"):
            if not _is_safe_status_url(feeder["status_url"]):
                log.warning("feeder %r: rejecting status_url %r (must be http on loopback)",
                            feeder.get("name"), feeder["status_url"])
                return []
            return await _feeder_details_fr24(feeder["status_url"])
        if st == "piaware" and feeder.get("status_path"):
            if not _is_safe_status_path(feeder["status_path"]):
                log.warning("feeder %r: rejecting status_path %r (must be under %s/)",
                            feeder.get("name"), feeder["status_path"], config.FEEDER_STATUS_ROOT)
                return []
            return _feeder_details_piaware(feeder["status_path"])
        if st == "mlat":
            return await _feeder_details_mlat(feeder["unit"])
    except Exception:
        # audit-12 #151 — surface real failures to the operator instead of
        # silently returning []. A misconfigured feeder or a corrupted
        # status file would otherwise be invisible.
        log.warning(
            "feeder %r: details fetch failed (status_type=%r)",
            feeder.get("name"), st, exc_info=True,
        )
    return []


async def _check_single_feeder(feeder: dict) -> dict:
    result = {"name": feeder["name"], "unit": feeder["unit"]}
    coros: list = [_check_systemd_unit(feeder["unit"])]
    if feeder.get("port"):
        coros.append(_check_port(feeder["port"]))
    checks = await asyncio.gather(*coros)
    for check in checks:
        result.update(check)
    systemd_ok = result.get("systemd") == "active"
    port_ok = result.get("port_status", "open") == "open"
    if result.get("systemd") == "unavailable":
        result["overall"] = "unknown"
    elif systemd_ok and port_ok:
        result["overall"] = "ok"
    else:
        result["overall"] = "error"
    result["details"] = await _fetch_feeder_details(feeder)
    return result


async def _check_all_feeders() -> list[dict]:
    return await asyncio.gather(*[_check_single_feeder(f) for f in config.FEEDERS])


@router.get("/api/feeders")
async def api_feeders() -> dict:
    """Same shape as the Jinja /feeders template uses — list of feeder
    status dicts plus a has_feeders flag for the empty-state notice.

    Results are cached for a short TTL (``cache._CACHE_TTLS["feeders"]``) and
    the batch is guarded by an asyncio.Lock so concurrent requests coalesce
    onto one subprocess fan-out instead of N (BE-18).
    """
    if not config.FEEDERS:
        return {"feeders": [], "has_feeders": False}
    cached = cache._get_cache("feeders")
    if cached is not None:
        return cached
    async with cache._feeder_lock():
        # Re-check under the lock: a batch that finished while we were waiting
        # has already populated the cache, so we skip re-spawning subprocesses.
        cached = cache._get_cache("feeders")
        if cached is not None:
            return cached
        feeders = list(await _check_all_feeders())
        result = {"feeders": feeders, "has_feeders": True}
        cache._set_cache("feeders", result)
        return result
