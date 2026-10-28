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


def test_mcp_env_example_exists_and_names_are_real_settings():
    example = SYSTEMD / "readsbstats-mcp.env.example"
    assert example.is_file()
    src = (ROOT / "src/readsbstats/config.py").read_text()
    names = set(re.findall(r"\bRSBS_[A-Z0-9_]+\b", example.read_text()))
    assert names, "example lists no settings"
    for name in names:
        assert f'"{name}"' in src, f"{name} is not a setting in config.py"


def test_mcp_env_example_has_no_active_values():
    for ln in _lines(SYSTEMD / "readsbstats-mcp.env.example"):
        assert not re.match(r"^RSBS_[A-Z0-9_]+=", ln), f"uncommented assignment: {ln}"


def test_mcp_settings_live_only_in_the_mcp_example():
    assert "RSBS_MCP_" not in (SYSTEMD / "readsbstats.env.example").read_text()


def test_install_sh_creates_env_file_mode_600():
    sh = (ROOT / "scripts/install.sh").read_text()
    assert "systemd/readsbstats.env.example" in sh
    assert re.search(rf"chmod 600 {re.escape(ENV_FILE)}", sh)
    assert re.search(rf"chown root:root {re.escape(ENV_FILE)}", sh)
    # Guarded: an operator's existing file is never overwritten.
    assert re.search(rf"if \[ ! -f {re.escape(ENV_FILE)} \]", sh)


# ---- readsbstats-mcp.service (opt-in, ADR-0022) ------------------------------
MCP_UNIT = SYSTEMD / "readsbstats-mcp.service"


MCP_ENV = "/etc/readsbstats/mcp.env"


def test_mcp_unit_reads_only_its_own_env_file():
    # The shared readsbstats.env carries the Telegram token and other secrets
    # this process must not get; the MCP unit has its own optional file.
    lines = _lines(MCP_UNIT)
    assert f"EnvironmentFile=-{MCP_ENV}" in lines
    env_lines = [ln for ln in lines if ln.startswith("EnvironmentFile=")]
    assert env_lines == [f"EnvironmentFile=-{MCP_ENV}"]
    assert "readsbstats.env" not in "\n".join(ln for ln in lines if not ln.startswith("#"))


def test_mcp_unit_runs_module_as_its_own_dynamic_user():
    lines = _lines(MCP_UNIT)
    assert "DynamicUser=yes" in lines
    assert "SupplementaryGroups=readsbstats" in lines
    assert not any(ln.startswith(("User=", "Group=")) for ln in lines)
    exec_start = [ln for ln in lines if ln.startswith("ExecStart=")]
    assert len(exec_start) == 1
    assert "-m readsbstats.mcp_server" in exec_start[0]


def test_mcp_unit_gets_tokens_through_a_credential():
    lines = _lines(MCP_UNIT)
    assert "LoadCredential=mcp-tokens:/etc/readsbstats/mcp-tokens" in lines
    assert "Environment=RSBS_MCP_TOKENS_FILE=%d/mcp-tokens" in lines


def test_mcp_unit_cannot_reach_the_database_directory():
    lines = _lines(MCP_UNIT)
    assert not any(ln.startswith("ReadWritePaths") for ln in lines)
    assert "InaccessiblePaths=-@DATA_DIR@" in lines
    assert not any(ln.startswith(("RequiresMountsFor", "Environment=RSBS_DATA_DIR"))
                   for ln in lines)


def test_mcp_unit_hardening_and_limits():
    lines = _lines(MCP_UNIT)
    assert "ProtectSystem=strict" in lines
    assert "NoNewPrivileges=yes" in lines
    assert "CPUQuota=25%" in lines
    assert "MemoryMax=256M" in lines


def test_mcp_unit_starts_after_web():
    lines = _lines(MCP_UNIT)
    after = next(ln for ln in lines if ln.startswith("After="))
    assert "readsbstats-web.service" in after.split()
    assert "Wants=readsbstats-web.service" in lines


def test_install_sh_installs_mcp_unit_but_never_enables_it():
    sh = (ROOT / "scripts/install.sh").read_text()
    # Rendered with the whole systemd/ dir (includes readsbstats-mcp.service).
    assert 'render_units "$DATA_DIR" "$APP_DIR/systemd"' in sh
    code = [ln for ln in sh.splitlines() if not ln.strip().startswith("#")]
    assert not any("enable" in ln and "readsbstats-mcp" in ln for ln in code)


# ---- configurable data directory (RSBS_DATA_DIR) -----------------------------
DATA_UNITS = ("collector", "web", "vdl2", "updater", "dbcheck", "dbcheck-full")


def _unit(name: str) -> Path:
    return SYSTEMD / f"readsbstats-{name}.service"


def test_data_units_use_the_data_dir_placeholder():
    for name in DATA_UNITS:
        lines = _lines(_unit(name))
        assert "RequiresMountsFor=@DATA_DIR@" in lines, name
        assert "ReadWritePaths=@DATA_DIR@" in lines, name
        assert "Environment=RSBS_DATA_DIR=@DATA_DIR@" in lines, name


def test_requires_mounts_for_sits_in_the_unit_section():
    for name in DATA_UNITS:
        text = _unit(name).read_text()
        unit_sec = text.split("[Service]")[0]
        assert "RequiresMountsFor=@DATA_DIR@" in unit_sec, name


def test_mcp_unit_has_no_mount_ordering_or_data_env():
    text = MCP_UNIT.read_text()
    assert "RequiresMountsFor" not in text
    assert "RSBS_DATA_DIR" not in text


def test_collector_and_vdl2_resource_limits():
    col = _lines(_unit("collector"))
    assert "CPUQuota=50%" in col and "MemoryMax=1G" in col
    vdl2 = _lines(_unit("vdl2"))
    assert "CPUQuota=50%" in vdl2 and "MemoryMax=768M" in vdl2
    for lines in (col, vdl2):
        assert not any(ln.startswith("MemoryHigh") for ln in lines)
        assert "Nice=10" in lines


def test_no_hardcoded_personal_mount_in_shipped_files():
    needle = "/mnt/" + "ext"
    offenders = []
    for sub in ("systemd", "scripts", "src", "tests"):
        for path in (ROOT / sub).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix in (".pyc", ".db"):
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if needle in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_env_example_documents_data_dir():
    text = (SYSTEMD / "readsbstats.env.example").read_text()
    assert "# RSBS_DATA_DIR=/var/lib/readsbstats" in text


# ---- updater + dbcheck scheduling (2026-10-28 incident) ----------------------

def test_updater_timer_does_not_fire_on_boot():
    # OnBootSec held the collector down ~3 min after every boot; Persistent=
    # already catches up a missed Monday run.
    lines = _lines(SYSTEMD / "readsbstats-updater.timer")
    assert not any(ln.startswith("OnBootSec=") for ln in lines)
    assert "Persistent=true" in lines


def test_updater_skips_while_collector_is_starting():
    lines = _lines(SYSTEMD / "readsbstats-updater.service")
    cond = [ln for ln in lines if ln.startswith("ExecCondition=")]
    assert len(cond) == 1
    assert "readsbstats-collector.service" in cond[0] and "activating" in cond[0]


def test_updater_not_ordered_after_collector():
    # After= on the collector would deadlock ExecStopPost's `systemctl start
    # readsbstats-collector`: systemd orders the updater's stop job before the
    # collector's start job, and that stop job is waiting on ExecStopPost.
    for ln in _lines(SYSTEMD / "readsbstats-updater.service"):
        if ln.startswith(("After=", "Before=")):
            assert "readsbstats-collector" not in ln, ln


def _timeout_start(unit: str) -> int:
    ln = next(ln for ln in _lines(SYSTEMD / unit) if ln.startswith("TimeoutStartSec="))
    return int(ln.split("=", 1)[1])


def test_dbcheck_timeouts_fit_a_cold_large_db():
    assert _timeout_start("readsbstats-dbcheck.service") >= 1800
    assert _timeout_start("readsbstats-dbcheck-full.service") >= 3600


def test_updater_restarts_collector_without_blocking():
    post = [ln for ln in _lines(SYSTEMD / "readsbstats-updater.service")
            if ln.startswith("ExecStopPost=")]
    assert post == ["ExecStopPost=+/bin/systemctl --no-block start readsbstats-collector.service"]
