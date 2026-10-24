"""systemd unit files + install.sh — single settings file invariants.

Every service reads its RSBS_* settings from one file,
/etc/readsbstats/readsbstats.env. The documented `tee -a` into it must take
effect on a fresh install, and notify-telegram@ (which requires the file) must
be able to start.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "systemd"
ENV_FILE = "/etc/readsbstats/readsbstats.env"
OPTIONAL_LINE = f"EnvironmentFile=-{ENV_FILE}"


def _lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines()]


def test_collector_and_web_read_optional_env_file():
    for unit in ("readsbstats-collector.service", "readsbstats-web.service"):
        assert OPTIONAL_LINE in _lines(SYSTEMD / unit), unit


def test_notify_telegram_still_requires_same_file():
    assert f"EnvironmentFile={ENV_FILE}" in _lines(SYSTEMD / "notify-telegram@.service")


def test_web_keeps_vdl2_env_file():
    assert "EnvironmentFile=-/etc/readsbstats/vdl2.env" in _lines(SYSTEMD / "readsbstats-web.service")


def test_vdl2_env_loads_after_settings_file_and_example_says_so():
    # systemd lets the later EnvironmentFile win: vdl2.env (which ships an active
    # RSBS_VDL2_ENABLED=false) overrides readsbstats.env for VDL2 vars, so the
    # settings example must point VDL2 settings at vdl2.env.
    lines = _lines(SYSTEMD / "readsbstats-web.service")
    assert lines.index(OPTIONAL_LINE) < lines.index("EnvironmentFile=-/etc/readsbstats/vdl2.env")
    assert "vdl2.env" in (SYSTEMD / "readsbstats.env.example").read_text()


def test_env_example_exists_and_names_are_real_settings():
    example = SYSTEMD / "readsbstats.env.example"
    assert example.is_file()
    src = (ROOT / "src/readsbstats/config.py").read_text() + (
        ROOT / "src/readsbstats/api/_deps.py").read_text()
    names = set(re.findall(r"\bRSBS_[A-Z0-9_]+\b", example.read_text()))
    assert names, "example lists no settings"
    for name in names:
        assert f'"{name}"' in src, f"{name} is not a setting in config.py/_deps.py"


def test_env_example_has_no_active_secret_values():
    # Shipping the example must not enable anything: every assignment is
    # commented out, and no secret is pre-filled.
    for ln in _lines(SYSTEMD / "readsbstats.env.example"):
        assert not re.match(r"^RSBS_[A-Z0-9_]+=", ln), f"uncommented assignment: {ln}"


def test_install_sh_creates_env_file_mode_600():
    sh = (ROOT / "scripts/install.sh").read_text()
    assert "systemd/readsbstats.env.example" in sh
    assert re.search(rf"chmod 600 {re.escape(ENV_FILE)}", sh)
    assert re.search(rf"chown root:root {re.escape(ENV_FILE)}", sh)
    # Guarded: an operator's existing file is never overwritten.
    assert re.search(rf"if \[ ! -f {re.escape(ENV_FILE)} \]", sh)
