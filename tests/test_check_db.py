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
