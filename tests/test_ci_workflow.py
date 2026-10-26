"""CI workflow invariants for the Playwright UI job.

The UI suite runs inside the official Playwright image, which ships the
browsers and their system libraries, so CI never apt-installs them (the Azure
apt mirror behind `playwright install --with-deps` was slow enough to time
the job out, and the retry around it could not work: the timed-out apt-get
ran as root and kept the dpkg lock).

The image's browser builds only match one Playwright release, so the Python
package is pinned exactly and must equal the image tag. Dependabot bumps the
pin but not the image; this test fails that PR until the tag is bumped too.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
PYPROJECT = ROOT / "pyproject.toml"

_IMAGE_RE = re.compile(r"image:\s*mcr\.microsoft\.com/playwright/python:v(\d+\.\d+\.\d+)-noble\b")
_PIN_RE = re.compile(r'"playwright==(\d+\.\d+\.\d+)"')


def _image_versions() -> list[str]:
    return _IMAGE_RE.findall(WORKFLOW.read_text())


def test_ui_job_runs_in_playwright_image():
    assert len(_image_versions()) == 1, "expected exactly one Playwright container image"


def test_playwright_pin_is_exact():
    assert len(_PIN_RE.findall(PYPROJECT.read_text())) == 1, \
        'pyproject ui-tests must pin "playwright==X.Y.Z" (the image only carries that release\'s browsers)'


def test_playwright_pin_matches_image_tag():
    pins = _PIN_RE.findall(PYPROJECT.read_text())
    assert pins and _image_versions() == pins, (
        f"pyproject pins playwright {pins} but test.yml uses image {_image_versions()}; "
        "bump both together")


def test_workflow_never_installs_browsers():
    # No `playwright install` (with or without --with-deps): the image has them.
    assert "playwright install" not in WORKFLOW.read_text()
