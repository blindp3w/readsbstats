"""Largest-Triangle-Three-Buckets visual downsampling.

Reduces a long, sorted-by-x timeseries to ``target`` points while
preserving the visual shape (peaks, troughs, slope changes). Returns
indices into the original list so callers can pluck *aligned* values
across multiple parallel series (altitude, speed, lat/lon) with the
same row selection.

Reference: Sveinn Steinarsson, "Downsampling Time Series for Visual
Representation", 2013 (Algorithm M4-2).
"""

from __future__ import annotations

from typing import Sequence


def lttb_indices(points: Sequence[tuple[float, float]], target: int) -> list[int]:
    """Pick ``target`` indices from ``points`` using the LTTB algorithm.

    ``points`` must be sorted by x (typically a timestamp). Each entry
    is ``(x, y)``. ``target`` is the desired output size; values <= 2 or
    >= ``len(points)`` short-circuit to "no downsampling needed".

    Always preserves the first and last input indices. Output is
    sorted ascending so callers can iterate the resulting selection in
    timeline order without re-sorting.
    """
    n = len(points)
    if target >= n or target <= 2 or n <= 2:
        return list(range(n))

    bucket_size = (n - 2) / (target - 2)

    out: list[int] = [0]  # always include first point
    a = 0                 # index of last selected point

    for i in range(target - 2):
        # Range of the *next* bucket — used to compute its centroid for
        # the triangle area calculation.
        next_start = int((i + 1) * bucket_size) + 1
        next_end   = min(int((i + 2) * bucket_size) + 1, n)
        if next_end <= next_start:
            next_end = next_start + 1
        avg_x = 0.0
        avg_y = 0.0
        for k in range(next_start, next_end):
            avg_x += points[k][0]
            avg_y += points[k][1]
        avg_x /= (next_end - next_start)
        avg_y /= (next_end - next_start)

        # Range of the *current* bucket — we pick one index from it.
        cur_start = int(i * bucket_size) + 1
        cur_end   = min(int((i + 1) * bucket_size) + 1, n)
        if cur_end <= cur_start:
            cur_end = cur_start + 1

        ax, ay = points[a]
        best_idx = cur_start
        best_area = -1.0
        for k in range(cur_start, cur_end):
            bx, by = points[k]
            # Triangle area with vertices a, k, next-bucket centroid.
            area = abs((ax - avg_x) * (by - ay) - (ax - bx) * (avg_y - ay))
            if area > best_area:
                best_area = area
                best_idx = k

        out.append(best_idx)
        a = best_idx

    out.append(n - 1)  # always include last point
    return out
