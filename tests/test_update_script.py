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
