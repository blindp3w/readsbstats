"""Journal noise: httpx logs every request at INFO, which doubled the
collector's journal volume. Each service entry module must quiet it."""
from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.fixture(autouse=True)
def setup():
    yield


@pytest.mark.parametrize("module", ["collector", "web", "db_updater", "vdl2_collector"])
def test_httpx_loggers_quieted_on_import(module):
    # Fresh interpreter: independent of whatever other tests did to the
    # process-wide logging state.
    code = (
        "import logging, readsbstats.%s;"
        "print(logging.getLogger('httpx').getEffectiveLevel(),"
        " logging.getLogger('httpcore').getEffectiveLevel())" % module
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True).stdout.split()
    assert all(int(x) >= 30 for x in out), out  # 30 == logging.WARNING
