"""scripts/update.sh — deploy-script invariants that shellcheck can't see."""
from __future__ import annotations

import re
from pathlib import Path

UPDATE_SH = (Path(__file__).resolve().parents[1] / "scripts" / "update.sh").read_text()


def _pip_install_lines() -> list[str]:
    return [
        line.strip()
        for line in UPDATE_SH.splitlines()
        if re.search(r"venv/bin/pip\"? install", line) and not line.strip().startswith("#")
    ]


def test_requirements_and_package_install_in_one_pip_call():
    """Installing requirements.txt first, then `-e .`, made pip's post-install
    check compare the NEW pins against the still-installed OLD package metadata
    and print a spurious "readsbstats requires fastapi==<old>" conflict on
    every deploy that bumps a pin. One invocation resolves both together."""
    lines = _pip_install_lines()
    assert len(lines) == 1, f"expected a single pip install, got: {lines}"
    assert "-r" in lines[0] and "requirements.txt" in lines[0]
    assert "-e" in lines[0]


def test_update_installs_units_by_rendering_not_copying():
    """The mcp (and every other) unit is installed through render_units, which
    substitutes @DATA_DIR@; a plain `cp` would ship the literal placeholder."""
    assert 'render_units "$DATA_DIR" "$APP_DIR/systemd"' in UPDATE_SH
    assert not re.search(r'^\s*cp "\$APP_DIR/systemd/[^"]*\.(service|timer)"',
                         UPDATE_SH, re.M)


def test_update_resolves_data_dir_before_backup_rsync_and_migration():
    resolve = UPDATE_SH.index("\nresolve_data_dir\n")
    for marker in ("VACUUM INTO", "rsync -a --delete", "migrate_v6.py"):
        assert resolve < UPDATE_SH.index(marker), marker
    assert 'DB_FILE="$DATA_DIR/history.db"' in UPDATE_SH
    assert "Data dir: $DATA_DIR (source: $DATA_DIR_SOURCE)" in UPDATE_SH


def test_update_aborts_for_an_existing_install_without_history_db():
    guard = re.search(
        r'if \[\[ -f "\$UNIT_DEST/readsbstats-collector\.service" '
        r'&& ! -f "\$DB_FILE" \]\]; then.*?exit 1\nfi', UPDATE_SH, re.S)
    assert guard, "missing-history.db guard not found"
    assert guard.start() < UPDATE_SH.index("VACUUM INTO")
    assert "RSBS_DATA_DIR" in guard.group(0)


def test_update_restarts_mcp_only_if_active_and_after_web():
    block = re.search(
        r"if systemctl is-active --quiet readsbstats-mcp; then\n"
        r"\s+systemctl restart readsbstats-mcp\n\s*fi",
        UPDATE_SH)
    assert block, "mcp restart must be guarded by is-active"
    # Every restart of the unit lives inside that guarded block.
    assert UPDATE_SH.count("systemctl restart readsbstats-mcp") == 1
    web = UPDATE_SH.index("systemctl restart readsbstats-collector readsbstats-web")
    assert web < block.start()


def test_install_sh_installs_mcp_unit_without_enabling():
    sh = (Path(__file__).resolve().parents[1] / "scripts" / "install.sh").read_text()
    # Installed by rendering the whole systemd/ dir (no per-unit cp).
    assert 'render_units "$DATA_DIR" "$APP_DIR/systemd"' in sh
    assert "readsbstats-mcp" in sh  # opt-in comment block stays
    assert not re.search(r"^[^#\n]*enable[^#\n]*readsbstats-mcp", sh, re.M)


def _runuser_commands(text: str) -> list[str]:
    """Each `runuser ...` command with its `\\` continuation lines joined."""
    cmds, cur = [], None
    for line in text.splitlines():
        s = line.strip()
        if cur is None and s.startswith("runuser "):
            cur = s
        elif cur is not None:
            cur += " " + s
        else:
            continue
        if not cur.endswith("\\"):
            cmds.append(cur)
            cur = None
    return cmds


def test_runuser_python_gets_the_data_dir():
    """runuser does not read readsbstats.env, so a Python command it starts
    would fall back to the default /var/lib/readsbstats and miss (or worse,
    create) the database. Every one must be handed the resolved data dir."""
    install_sh = (Path(__file__).resolve().parents[1] / "scripts" / "install.sh").read_text()
    for text in (UPDATE_SH, install_sh):
        cmds = [c for c in _runuser_commands(text) if "venv/bin/python" in c]
        assert cmds
        for cmd in cmds:
            assert 'env RSBS_DATA_DIR="$DATA_DIR"' in cmd, cmd
