"""Coverage for the cache prewarm-schedule ordering (audit 2026-06-20 gap).

`_initial_prewarm_schedule` was extracted specifically to make its ordering
unit-testable without spinning up the prewarmer thread or sleeping. A broken
priority order would ship silently as a colder cache after restart.
"""
from readsbstats.cache import (
    _initial_prewarm_schedule,
    _PREWARM_TARGETS,
    _PREWARM_TTL_PRIORITY,
)


def test_initial_prewarm_schedule_orders_stats_first_then_ttl():
    keys = list(_initial_prewarm_schedule(_PREWARM_TARGETS, now=0.0))
    # The single stats payload warms first (a cold "All time" click is the
    # slowest ~15-query scan).
    assert keys[0] == ("stats", "all")
    # Remaining (map) targets run by ascending TTL priority...
    map_ttls = [_PREWARM_TTL_PRIORITY[w] for (_k, w) in keys[1:]]
    assert map_ttls == sorted(map_ttls)
    # ...and heatmap before coverage at the same TTL window.
    for (k1, w1), (k2, w2) in zip(keys[1:], keys[2:]):
        if w1 == w2:
            assert not (k1 == "coverage" and k2 == "heatmap")


def test_initial_prewarm_schedule_staggers_times():
    sched = _initial_prewarm_schedule(_PREWARM_TARGETS, now=1000.0)
    times = list(sched.values())
    assert times[0] == 1000.0          # first target at `now`
    assert times == sorted(times)      # monotonic
    assert len(set(times)) == len(times)  # all distinct (staggered)
