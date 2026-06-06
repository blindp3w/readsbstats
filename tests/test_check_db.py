"""Tests for the scripts/check_db.py integrity checker.

Covers the URI-escaping fix: paths containing ``?``, ``#`` or spaces must
be properly percent-encoded before being embedded in a SQLite URI, or
the URI parser misinterprets them as parameters/fragments and opens the
wrong file (or fails entirely).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


_CHECK_DB_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_db.py"
)


def _load_check_db_module():
    spec = importlib.util.spec_from_file_location("check_db", _CHECK_DB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_minimal_db(path: Path) -> None:
    """Create a tiny, valid SQLite file at ``path``."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "filename",
    [
        "history.db",
        "with space.db",
        "with?question.db",
        "with#hash.db",
        "with%percent.db",
    ],
)
def test_check_db_opens_special_paths(filename, tmp_path, capsys, monkeypatch):
    """check_db.py must open the file at the literal path even when the
    path contains URI metacharacters.

    Regression: the original ``sqlite3.connect(f"file:{args.db}?mode=ro",
    uri=True)`` let ``?`` / ``#`` split the URI into bogus
    parameters/fragments. SQLite then opened a *different* path (with
    the metacharacter and everything after it stripped) in default
    rwc mode, creating an empty spurious file. PRAGMA quick_check on
    the empty file returned "ok" so the script reported success while
    silently looking at the wrong place.

    This test asserts two things:
    1. The script exits 0 (quick_check passes on the real DB).
    2. The script does not create any new files in the temp dir — i.e.
       it actually opens the parametrised path, not a truncated alias.
    """
    db_path = tmp_path / filename
    _make_minimal_db(db_path)
    files_before = sorted(p.name for p in tmp_path.iterdir())

    check_db = _load_check_db_module()
    monkeypatch.setattr(sys, "argv", ["check_db.py", "--db", str(db_path)])

    with pytest.raises(SystemExit) as exc:
        check_db.main()

    assert exc.value.code == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert "OK" in out

    files_after = sorted(p.name for p in tmp_path.iterdir())
    assert files_after == files_before, (
        f"check_db.py created a spurious file: "
        f"before={files_before} after={files_after}"
    )


# ---------------------------------------------------------------------------
# Audit 17: the failure-exit branches are the systemd `dbcheck` alarm — a
# regression that reports clean on real corruption would silently disable the
# only corruption alarm. Pin exit 1 (corruption) and exit 2 (open/query error).
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows


class _FakeConn:
    def __init__(self, rows=None, raise_on_pragma=False):
        self._rows = rows or []
        self._raise = raise_on_pragma

    def execute(self, sql, *a):
        if sql.startswith(("PRAGMA quick_check", "PRAGMA integrity_check")):
            if self._raise:
                raise sqlite3.DatabaseError("disk I/O error")
            return _FakeCursor(self._rows)
        return _FakeCursor([])

    def close(self): pass


def test_open_error_exits_2(tmp_path, capsys, monkeypatch):
    """A DB that can't be opened read-only (e.g. missing file) exits 2."""
    check_db = _load_check_db_module()
    missing = tmp_path / "does_not_exist.db"
    monkeypatch.setattr(sys, "argv", ["check_db.py", "--db", str(missing)])
    with pytest.raises(SystemExit) as exc:
        check_db.main()
    assert exc.value.code == 2
    assert "cannot open" in capsys.readouterr().err


def test_corruption_exits_1(tmp_path, capsys, monkeypatch):
    """quick_check returning non-'ok' rows exits 1 with the issue count."""
    check_db = _load_check_db_module()
    rows = [("row 9 missing from index idx_x",), ("page 5 corrupt",)]
    monkeypatch.setattr(check_db.sqlite3, "connect", lambda *a, **k: _FakeConn(rows))
    monkeypatch.setattr(sys, "argv", ["check_db.py", "--db", str(tmp_path / "x.db")])
    with pytest.raises(SystemExit) as exc:
        check_db.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "CORRUPTION DETECTED" in err
    assert "2 issue(s)" in err


def test_corruption_truncates_issue_list_at_20(tmp_path, capsys, monkeypatch):
    """More than 20 issues are truncated with an '… and N more' summary."""
    check_db = _load_check_db_module()
    rows = [(f"issue {i}",) for i in range(25)]
    monkeypatch.setattr(check_db.sqlite3, "connect", lambda *a, **k: _FakeConn(rows))
    monkeypatch.setattr(sys, "argv", ["check_db.py", "--db", str(tmp_path / "x.db")])
    with pytest.raises(SystemExit) as exc:
        check_db.main()
    assert exc.value.code == 1
    assert "… and 5 more" in capsys.readouterr().err


def test_query_error_exits_2(tmp_path, capsys, monkeypatch):
    """A sqlite error raised by the PRAGMA itself exits 2 (not 1)."""
    check_db = _load_check_db_module()
    monkeypatch.setattr(check_db.sqlite3, "connect",
                        lambda *a, **k: _FakeConn(raise_on_pragma=True))
    monkeypatch.setattr(sys, "argv", ["check_db.py", "--db", str(tmp_path / "x.db")])
    with pytest.raises(SystemExit) as exc:
        check_db.main()
    assert exc.value.code == 2
    assert "ERROR:" in capsys.readouterr().err
