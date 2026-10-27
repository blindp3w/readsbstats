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


def test_update_copies_mcp_unit():
    assert re.search(
        r'^cp "\$APP_DIR/systemd/readsbstats-mcp\.service"\s+/etc/systemd/system/',
        UPDATE_SH, re.M)


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


def test_install_sh_copies_mcp_unit_without_enabling():
    sh = (Path(__file__).resolve().parents[1] / "scripts" / "install.sh").read_text()
    assert 'systemd/readsbstats-mcp.service"' in sh
    assert not re.search(r"^[^#\n]*enable[^#\n]*readsbstats-mcp", sh, re.M)
