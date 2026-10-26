"""Suite-wide pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _reset_notifier_telegram_cache(monkeypatch):
    """``notifier.telegram_enabled()`` caches its validation result in two
    module globals. Reset them around every test so the outcome never depends
    on which test ran first (monkeypatch restores the originals on teardown,
    and the originals are the pristine ``None`` / ``False``)."""
    from readsbstats import notifier
    monkeypatch.setattr(notifier, "_tg_enabled", None)
    monkeypatch.setattr(notifier, "_tg_validated", False)
    yield


@pytest.fixture(scope="session", autouse=True)
def _close_shared_event_loop():
    """Close the loop behind ``tests._helpers.run_async`` at session end so no
    unclosed-loop ResourceWarning is emitted."""
    yield
    from tests._helpers import close_async_loop
    close_async_loop()
