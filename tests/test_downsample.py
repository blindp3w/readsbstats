"""Tests for the LTTB visual downsampling helper."""

from __future__ import annotations

import math
import random

import pytest

from readsbstats.downsample import lttb_indices


def test_short_input_returns_all_indices():
    points = [(0.0, 0.0), (1.0, 1.0)]
    assert lttb_indices(points, 10) == [0, 1]


def test_target_zero_or_one_short_circuits():
    points = [(i, i) for i in range(100)]
    # Trivial targets bypass LTTB and return every index — the algorithm
    # is undefined for target <= 2 because it can't form a bucket.
    assert lttb_indices(points, 0) == list(range(100))
    assert lttb_indices(points, 1) == list(range(100))


def test_target_equals_input_size_returns_all():
    points = [(i, i) for i in range(50)]
    assert lttb_indices(points, 50) == list(range(50))


def test_target_exceeds_input_size_returns_all():
    points = [(i, i) for i in range(10)]
    assert lttb_indices(points, 1000) == list(range(10))


def test_output_length_matches_target():
    points = [(i, math.sin(i / 10.0)) for i in range(1000)]
    out = lttb_indices(points, 100)
    assert len(out) == 100


def test_output_indices_monotone_increasing():
    points = [(i, math.sin(i / 10.0)) for i in range(1000)]
    out = lttb_indices(points, 100)
    assert all(b > a for a, b in zip(out, out[1:])), (
        f"indices not monotone: {out}"
    )


def test_first_and_last_indices_always_preserved():
    points = [(i, random.random()) for i in range(500)]
    out = lttb_indices(points, 50)
    assert out[0] == 0
    assert out[-1] == 499


def test_single_spike_in_middle_survives_downsampling():
    """LTTB's triangle-area metric must pick the spike index in
    whichever bucket it lands in — that's the point of the algorithm."""
    n = 1000
    spike_idx = 500
    points = [(i, 0.0) for i in range(n)]
    points[spike_idx] = (spike_idx, 100.0)

    out = lttb_indices(points, 50)
    assert spike_idx in out, (
        f"spike at index {spike_idx} was dropped during downsampling: "
        f"output={out}"
    )


def test_all_indices_within_input_range():
    points = [(i, math.cos(i / 5.0)) for i in range(200)]
    out = lttb_indices(points, 30)
    assert all(0 <= idx < 200 for idx in out)
