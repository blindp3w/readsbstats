"""scripts/_deploy_lib.sh — data-dir resolution and unit rendering.

The library is sourced by install.sh / update.sh as root on the Pi; here it is
exercised through bash with RSBS_ENV_FILE / UNIT_DEST pointing into tmp_path.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "_deploy_lib.sh"
SYSTEMD = ROOT / "systemd"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


@pytest.fixture(autouse=True)
def setup(tmp_path):
    # tmp_path-based env + unit dir shared by every test via the helper below.
    yield


def _run(tmp_path, script: str, *, env_text: str | None = None,
         unit_text: str | None = None, extra_env: dict | None = None):
    env_file = tmp_path / "etc" / "readsbstats.env"
    unit_dir = tmp_path / "units"
    unit_dir.mkdir(exist_ok=True)
    if env_text is not None:
        env_file.parent.mkdir(exist_ok=True)
        env_file.write_text(env_text)
    if unit_text is not None:
        (unit_dir / "readsbstats-collector.service").write_text(unit_text)
    env = {
        "PATH": os.environ["PATH"],
        "RSBS_ENV_FILE": str(env_file),
        "UNIT_DEST": str(unit_dir),
    }
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source '{LIB}'; {script}"],
        env=env, capture_output=True, text=True,
    )
    return proc, env_file, unit_dir


def _resolve(tmp_path, **kw):
    proc, env_file, unit_dir = _run(
        tmp_path, 'resolve_data_dir; echo "$DATA_DIR|$DATA_DIR_SOURCE"', **kw)
    return proc, env_file, unit_dir


# ---- env_value ---------------------------------------------------------------

class TestEnvValue:
    def test_last_uncommented_line_wins(self, tmp_path):
        proc, _, _ = _run(tmp_path, "env_value RSBS_X", env_text=(
            "RSBS_X=one\n# RSBS_X=commented\n  RSBS_X=two\nRSBS_Y=other\n"))
        assert proc.stdout == "two\n"

    @pytest.mark.parametrize("raw", ['"/srv/a"', "'/srv/a'", "/srv/a"])
    def test_quotes_stripped(self, tmp_path, raw):
        proc, _, _ = _run(tmp_path, "env_value RSBS_X", env_text=f"RSBS_X={raw}\n")
        assert proc.stdout == "/srv/a\n"

    def test_commented_only_is_empty(self, tmp_path):
        proc, _, _ = _run(tmp_path, "env_value RSBS_X", env_text="# RSBS_X=/srv/a\n")
        assert proc.returncode == 0 and proc.stdout == ""

    def test_missing_file_is_empty_and_ok(self, tmp_path):
        proc, _, _ = _run(tmp_path, "env_value RSBS_X")
        assert proc.returncode == 0 and proc.stdout == ""

    def test_prefix_name_does_not_match(self, tmp_path):
        proc, _, _ = _run(tmp_path, "env_value RSBS_X", env_text="RSBS_XY=nope\n")
        assert proc.stdout == ""

    def test_file_without_trailing_newline(self, tmp_path):
        proc, _, _ = _run(tmp_path, "env_value RSBS_X", env_text="RSBS_X=/srv/a")
        assert proc.stdout == "/srv/a\n"

    def test_file_is_never_sourced(self, tmp_path):
        marker = tmp_path / "pwned"
        proc, _, _ = _run(tmp_path, "env_value RSBS_X",
                          env_text=f"touch {marker}\nRSBS_X=$(touch {marker})\n")
        assert not marker.exists()
        assert proc.returncode == 0


# ---- valid_data_dir ----------------------------------------------------------

class TestValidDataDir:
    @pytest.mark.parametrize("d", [
        "/var/lib/readsbstats", "/mnt/usb-disk/rsbs", "/srv/a.b_c", "/x",
        "/srv/..hidden/data",
    ])
    def test_accepts(self, tmp_path, d):
        proc, _, _ = _run(tmp_path, f"valid_data_dir '{d}'")
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize("d", [
        "relative/dir", "", "/", "/srv/with space", "/srv/a|b", "/srv/a@b",
        "/srv/../etc", "/..", "/srv/..", "/srv/a;b", "/srv/$HOME",
    ])
    def test_rejects(self, tmp_path, d):
        proc, _, _ = _run(tmp_path, f"valid_data_dir '{d}'")
        assert proc.returncode != 0

    def test_rejects_newline_injection(self, tmp_path):
        proc, _, _ = _run(
            tmp_path, r"valid_data_dir $'/srv/a\nExecStartPre=/bin/evil'")
        assert proc.returncode != 0


# ---- resolve_data_dir --------------------------------------------------------

class TestResolveDataDir:
    def test_env_file_wins_over_installed_unit(self, tmp_path):
        proc, env_file, _ = _resolve(
            tmp_path, env_text='RSBS_DATA_DIR="/srv/from-env"\n',
            unit_text="[Service]\nReadWritePaths=/srv/old\n")
        assert proc.stdout.strip() == "/srv/from-env|env"
        assert env_file.read_text() == 'RSBS_DATA_DIR="/srv/from-env"\n'  # untouched

    def test_default_when_nothing(self, tmp_path):
        proc, env_file, _ = _resolve(tmp_path)
        assert proc.stdout.strip() == "/var/lib/readsbstats|default"
        assert not env_file.exists()

    def test_default_ignores_templated_unit(self, tmp_path):
        proc, env_file, _ = _resolve(
            tmp_path, unit_text="[Service]\nReadWritePaths=@DATA_DIR@\n")
        assert proc.stdout.strip() == "/var/lib/readsbstats|default"
        assert not env_file.exists()

    def test_migration_from_installed_unit_persists_once(self, tmp_path):
        unit = "[Service]\nReadWritePaths=/srv/x\nReadOnlyPaths=/run/readsb\n"
        proc, env_file, _ = _resolve(tmp_path, env_text="RSBS_LAT=1.0\n",
                                     unit_text=unit)
        assert proc.stdout.strip() == "/srv/x|migrated"
        text = env_file.read_text()
        assert text == (
            "RSBS_LAT=1.0\n"
            "# Added by update.sh (v2.32.0): data directory migrated from the installed units.\n"
            "RSBS_DATA_DIR=/srv/x\n")
        # Second call: now sourced from the env file, nothing appended.
        proc2, _, _ = _resolve(tmp_path)
        assert proc2.stdout.strip() == "/srv/x|env"
        assert env_file.read_text() == text

    def test_migration_creates_missing_env_file_mode_600(self, tmp_path):
        proc, env_file, _ = _resolve(
            tmp_path, unit_text="[Service]\nReadWritePaths=/srv/x\n")
        assert proc.stdout.strip() == "/srv/x|migrated"
        assert (env_file.stat().st_mode & 0o777) == 0o600
        assert env_file.read_text().count("RSBS_DATA_DIR=") == 1

    def test_migration_appends_on_new_line_when_no_trailing_newline(self, tmp_path):
        proc, env_file, _ = _resolve(
            tmp_path, env_text="RSBS_LAT=1.0",
            unit_text="[Service]\nReadWritePaths=/srv/x\n")
        lines = env_file.read_text().splitlines()
        assert lines[0] == "RSBS_LAT=1.0"
        assert lines[-1] == "RSBS_DATA_DIR=/srv/x"

    def test_invalid_env_value_fails(self, tmp_path):
        proc, _, _ = _resolve(tmp_path, env_text="RSBS_DATA_DIR=relative/dir\n")
        assert proc.returncode != 0
        assert "ERROR" in proc.stderr

    def test_invalid_installed_unit_path_fails_without_persisting(self, tmp_path):
        proc, env_file, _ = _resolve(
            tmp_path, unit_text="[Service]\nReadWritePaths=/srv/a b\n")
        # whitespace in the unit value => treated as unusable, falls to default
        assert proc.stdout.strip() == "/var/lib/readsbstats|default"
        assert not env_file.exists()

    def test_parent_traversal_in_unit_path_rejected(self, tmp_path):
        proc, env_file, _ = _resolve(
            tmp_path, unit_text="[Service]\nReadWritePaths=/srv/../etc\n")
        assert proc.returncode != 0
        assert not env_file.exists()


# ---- render_units ------------------------------------------------------------

class TestRenderUnits:
    def test_renders_every_unit_without_placeholder(self, tmp_path):
        proc, _, unit_dir = _run(
            tmp_path, f"render_units /srv/rsbs '{SYSTEMD}'")
        assert proc.returncode == 0, proc.stderr
        sources = sorted(p.name for p in SYSTEMD.iterdir()
                         if p.suffix in (".service", ".timer"))
        assert sources, "no units found"
        assert sorted(p.name for p in unit_dir.iterdir()) == sources
        for name in sources:
            text = (unit_dir / name).read_text()
            assert "@DATA_DIR@" not in text, name
        for name in ("collector", "web", "vdl2", "updater", "dbcheck", "dbcheck-full"):
            text = (unit_dir / f"readsbstats-{name}.service").read_text()
            assert "ReadWritePaths=/srv/rsbs" in text
            assert "RequiresMountsFor=/srv/rsbs" in text
            assert "Environment=RSBS_DATA_DIR=/srv/rsbs" in text
        mcp = (unit_dir / "readsbstats-mcp.service").read_text()
        assert "InaccessiblePaths=-/srv/rsbs" in mcp

    def test_default_dir_render_only_changes_placeholder_lines(self, tmp_path):
        proc, _, unit_dir = _run(
            tmp_path, f"render_units /var/lib/readsbstats '{SYSTEMD}'")
        assert proc.returncode == 0, proc.stderr
        for src in SYSTEMD.glob("*.service"):
            expect = src.read_text().replace("@DATA_DIR@", "/var/lib/readsbstats")
            assert (unit_dir / src.name).read_text() == expect

    def test_no_temp_files_left_behind(self, tmp_path):
        _, _, unit_dir = _run(tmp_path, f"render_units /srv/rsbs '{SYSTEMD}'")
        assert not [p for p in unit_dir.iterdir() if p.name.startswith(".")]

    @pytest.mark.parametrize("bad", ["rel", "/a b", "/a/../b", ""])
    def test_refuses_invalid_dir(self, tmp_path, bad):
        proc, _, unit_dir = _run(tmp_path, f"render_units '{bad}' '{SYSTEMD}'")
        assert proc.returncode != 0
        assert not list(unit_dir.iterdir())

    def test_fails_if_placeholder_survives(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        # Simulate a substitution that silently does nothing (sed stubbed to cat).
        (src / "x.service").write_text("[Service]\nReadWritePaths=@DATA_DIR@\n")
        proc, _, unit_dir = _run(
            tmp_path, f"sed() {{ cat \"$2\"; }}; render_units /srv/rsbs '{src}'")
        assert proc.returncode != 0
        assert "still contains" in proc.stderr
        assert not (unit_dir / "x.service").exists()


# ---- text-level invariants of the scripts ------------------------------------

class TestScriptsWiring:
    def test_lib_is_shellcheck_annotated_and_has_no_sed_inplace(self):
        text = LIB.read_text()
        assert text.startswith("# shellcheck shell=bash")
        assert not re.search(r"\bsed\s+-i", text)

    @pytest.mark.parametrize("script", ["install.sh", "update.sh"])
    def test_scripts_source_lib_and_render_units(self, script):
        sh = (ROOT / "scripts" / script).read_text()
        assert "_deploy_lib.sh" in sh
        assert 'render_units "$DATA_DIR" "$APP_DIR/systemd"' in sh
        assert not re.search(r'^\s*cp "\$APP_DIR/systemd/[^"]*\.(service|timer)"', sh, re.M)
        assert "/mnt/" + "ext" not in sh


# ---- keep_backups_value / prune_backups --------------------------------------

def _mk_backups(tmp_path, stamps, db="history.db"):
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    for s in stamps:
        (d / f"{db}.backup.{s}").write_text("x")
    return d


def _left(d, db="history.db"):
    return sorted(p.name[len(db) + len(".backup."):] for p in d.glob(f"{db}.backup.*"))


STAMPS = ["20260101_000001", "20260102_000001", "20260103_000001",
          "20260104_000001", "20260105_000001"]


class TestKeepBackupsValue:
    def test_default_is_three(self, tmp_path):
        proc, _, _ = _run(tmp_path, "keep_backups_value", env_text="RSBS_X=1\n")
        assert proc.stdout == "3\n" and proc.stderr == ""

    @pytest.mark.parametrize("v", ["1", "5", "20"])
    def test_valid(self, tmp_path, v):
        proc, _, _ = _run(tmp_path, "keep_backups_value",
                          env_text=f"RSBS_KEEP_BACKUPS={v}\n")
        assert proc.stdout == f"{v}\n" and proc.stderr == ""

    @pytest.mark.parametrize("v", ["0", "-1", "abc", "2.5", "03", "99999999999999999999"])
    def test_invalid_falls_back_with_warning(self, tmp_path, v):
        proc, _, _ = _run(tmp_path, "keep_backups_value",
                          env_text=f"RSBS_KEEP_BACKUPS={v}\n")
        assert proc.returncode == 0
        assert proc.stdout == "3\n"
        assert "WARNING" in proc.stderr and "RSBS_KEEP_BACKUPS" in proc.stderr

    def test_empty_value_is_default(self, tmp_path):
        # `RSBS_KEEP_BACKUPS=` reads the same as unset (env_value prints nothing).
        proc, _, _ = _run(tmp_path, "keep_backups_value", env_text="RSBS_KEEP_BACKUPS=\n")
        assert proc.returncode == 0 and proc.stdout == "3\n"


class TestPruneBackups:
    def test_keeps_n_newest_by_timestamp(self, tmp_path):
        d = _mk_backups(tmp_path, STAMPS)
        proc, _, _ = _run(tmp_path, f"prune_backups '{d}/history.db' 2")
        assert proc.returncode == 0, proc.stderr
        assert _left(d) == STAMPS[-2:]

    def test_order_follows_name_not_mtime(self, tmp_path):
        d = _mk_backups(tmp_path, STAMPS)
        # Give the oldest-named backup the newest mtime: it must still go.
        os.utime(d / f"history.db.backup.{STAMPS[0]}", (4_000_000_000, 4_000_000_000))
        _run(tmp_path, f"prune_backups '{d}/history.db' 3")
        assert _left(d) == STAMPS[-3:]

    def test_fewer_than_keep_deletes_nothing(self, tmp_path):
        d = _mk_backups(tmp_path, STAMPS[:2])
        proc, _, _ = _run(tmp_path, f"prune_backups '{d}/history.db' 3")
        assert proc.returncode == 0 and _left(d) == STAMPS[:2]

    def test_no_backups_is_ok(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        proc, _, _ = _run(tmp_path, f"prune_backups '{d}/history.db' 3")
        assert proc.returncode == 0

    def test_other_files_untouched(self, tmp_path):
        d = _mk_backups(tmp_path, STAMPS)
        _mk_backups(tmp_path, STAMPS, db="vdl2.db")
        for name in ("history.db", "history.db.backup.pre-v6", "history.db.backup.",
                     "history.db.backup.20260101_000001.bak", "history.db-wal"):
            (d / name).write_text("keep")
        proc, _, _ = _run(tmp_path, f"prune_backups '{d}/history.db' 1")
        assert proc.returncode == 0, proc.stderr
        assert set(_left(d)) == {STAMPS[-1], "20260101_000001.bak", "pre-v6", ""}
        assert _left(d, "vdl2.db") == STAMPS
        for name in ("history.db", "history.db-wal"):
            assert (d / name).exists()

    @pytest.mark.parametrize("keep", ["0", "-1", "abc", ""])
    def test_refuses_invalid_keep(self, tmp_path, keep):
        d = _mk_backups(tmp_path, STAMPS)
        proc, _, _ = _run(tmp_path, f"prune_backups '{d}/history.db' '{keep}'")
        assert proc.returncode != 0
        assert _left(d) == STAMPS


def test_update_uses_keep_backups_helpers():
    text = (ROOT / "scripts" / "update.sh").read_text()
    assert 'KEEP_BACKUPS="$(keep_backups_value)"' in text
    assert 'prune_backups "$DB_FILE" "$KEEP_BACKUPS"' in text
    assert "-printf" not in text
