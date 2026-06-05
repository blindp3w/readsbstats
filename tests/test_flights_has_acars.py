"""Tests for the gated VDL2 `has_acars` column + filter on /api/flights.

Exercises the real ATTACH path: a tempfile vdl2.db attached to a tempfile
history.db (in-memory DBs can't be cross-attached). Verifies the column/filter
appear only when attached, and that a missing vdl2.db degrades to no column /
no 500 rather than erroring.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from readsbstats import config, database, web
from readsbstats.api import _deps
from readsbstats.vdl2 import db as vdl2_db


def _core_db(path: str):
    # uri=True mirrors production _deps.db(): the read-only `file:…?mode=ro`
    # ATTACH only resolves when the main connection was opened in URI mode.
    conn = database.connect(path, uri=True)
    conn.executescript(database.DDL)
    database._migrate(conn)
    return conn


def _flight(conn, icao, first_seen, last_seen):
    conn.execute(
        "INSERT INTO flights (icao_hex, callsign, first_seen, last_seen) VALUES (?,?,?,?)",
        (icao, "LO1", first_seen, last_seen),
    )
    conn.commit()


@pytest.fixture()
def attached(tmp_path, monkeypatch):
    """Core history.db with two flights + a vdl2.db attached; one flight's
    window contains an ACARS message, the other's does not."""
    vdl2_path = str(tmp_path / "vdl2.db")
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    monkeypatch.setattr(config, "VDL2_DB_PATH", vdl2_path)

    vconn = vdl2_db.connect(vdl2_path)
    vdl2_db.ensure_schema(vconn)
    vdl2_db.insert_messages(vconn, [{"ts": 1500, "icao_hex": "48e95d", "body": "hi"}])
    vconn.commit()
    vconn.close()

    core = _core_db(str(tmp_path / "history.db"))
    _flight(core, "48e95d", 1000, 2000)   # has ACARS at ts=1500
    _flight(core, "aabbcc", 1000, 2000)   # no ACARS
    _deps._maybe_attach_vdl2(core)
    assert _deps.vdl2_attached(core) is True
    monkeypatch.setattr(_deps, "_db", core)
    yield core
    core.close()


def test_has_acars_column(attached):
    with TestClient(web.app, headers={"X-Requested-With": "XMLHttpRequest"}) as c:
        data = c.get("/api/flights").json()
    by = {f["icao_hex"]: f for f in data["flights"]}
    assert by["48e95d"]["has_acars"] == 1
    assert by["aabbcc"]["has_acars"] == 0


def test_has_acars_filter_narrows_results_and_total(attached):
    with TestClient(web.app) as c:
        data = c.get("/api/flights?has_acars=true").json()
    assert data["total"] == 1
    assert [f["icao_hex"] for f in data["flights"]] == ["48e95d"]


def test_attach_is_read_only(attached):
    # The mode=ro attach must reject writes through vdl2db — enforced boundary,
    # not convention.
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        attached.execute("INSERT INTO vdl2db.vdl2_messages (ts) VALUES (1)")


def test_single_position_flight_boundary(tmp_path, monkeypatch):
    # first_seen == last_seen: the window must be inclusive (<=) so a message at
    # that exact second still counts as ACARS for the flight.
    vdl2_path = str(tmp_path / "vdl2.db")
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    monkeypatch.setattr(config, "VDL2_DB_PATH", vdl2_path)
    vconn = vdl2_db.connect(vdl2_path)
    vdl2_db.ensure_schema(vconn)
    vdl2_db.insert_messages(vconn, [{"ts": 1500, "icao_hex": "48e95d", "body": "x"}])
    vconn.commit()
    vconn.close()
    core = _core_db(str(tmp_path / "history.db"))
    _flight(core, "48e95d", 1500, 1500)   # single-sample flight at ts=1500
    _deps._maybe_attach_vdl2(core)
    monkeypatch.setattr(_deps, "_db", core)
    with TestClient(web.app) as c:
        data = c.get("/api/flights").json()
    assert data["flights"][0]["has_acars"] == 1
    core.close()


def test_attach_reattempted_when_vdl2_appears_later(tmp_path, monkeypatch):
    # BE-003: a vdl2.db that appears AFTER the core connection was created must
    # still attach on a later request (idempotent re-attach), not stay absent
    # until a web restart.
    vdl2_path = str(tmp_path / "vdl2.db")
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    monkeypatch.setattr(config, "VDL2_DB_PATH", vdl2_path)
    core = _core_db(str(tmp_path / "history.db"))
    _flight(core, "48e95d", 1000, 2000)
    _deps._maybe_attach_vdl2(core)
    assert _deps.vdl2_attached(core) is False        # nothing to attach yet
    monkeypatch.setattr(_deps, "_db", core)

    # vdl2.db is created later, with a message in the flight window.
    vconn = vdl2_db.connect(vdl2_path)
    vdl2_db.ensure_schema(vconn)
    vdl2_db.insert_messages(vconn, [{"ts": 1500, "icao_hex": "48e95d", "body": "hi"}])
    vconn.commit()
    vconn.close()

    with TestClient(web.app) as c:
        data = c.get("/api/flights").json()
    assert data["flights"][0]["has_acars"] == 1      # re-attached on this request
    core.close()


def test_no_column_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VDL2_ENABLED", False)
    core = _core_db(str(tmp_path / "history.db"))
    _flight(core, "48e95d", 1000, 2000)
    monkeypatch.setattr(_deps, "_db", core)
    with TestClient(web.app) as c:
        data = c.get("/api/flights").json()
    assert "has_acars" not in data["flights"][0]
    core.close()


def test_attach_failure_degrades_no_500(tmp_path, monkeypatch):
    # Enabled but vdl2.db cannot be attached (e.g. read-only mount / corrupt file)
    # → the flights endpoint must omit the column instead of erroring. The web
    # lifespan creates vdl2.db when enabled, so "file missing" isn't the realistic
    # trigger; simulate the genuine case — attach never succeeds — with a no-op
    # _maybe_attach_vdl2 (the handler now re-attempts the attach per request, BE-003).
    monkeypatch.setattr(config, "VDL2_ENABLED", True)
    monkeypatch.setattr(config, "VDL2_DB_PATH", str(tmp_path / "vdl2.db"))
    monkeypatch.setattr(_deps, "_maybe_attach_vdl2", lambda conn: None)
    core = _core_db(str(tmp_path / "history.db"))
    _flight(core, "48e95d", 1000, 2000)
    assert _deps.vdl2_attached(core) is False
    monkeypatch.setattr(_deps, "_db", core)
    with TestClient(web.app, raise_server_exceptions=True) as c:
        r = c.get("/api/flights")
    assert r.status_code == 200
    assert "has_acars" not in r.json()["flights"][0]
    core.close()
