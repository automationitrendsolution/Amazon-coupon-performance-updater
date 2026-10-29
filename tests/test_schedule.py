"""Campaign start windows: the daily window, windows given by hand, the run lock."""
from __future__ import annotations

from datetime import date

import pytest

from app.schedule import AlreadyRunning, PeriodError, RunLock, daily_window, parse_date, validate_window


def test_daily_window_matches_the_old_script():
    window = daily_window(date(2026, 10, 29), lookback_days=30, lookahead_days=10)
    assert (window.start, window.end) == (date(2026, 9, 29), date(2026, 11, 8))
    assert window.amazon() == ("2026-09-29T00:00:00Z", "2026-11-08T00:00:00Z")
    assert str(window) == "2026-09-29..2026-11-08"


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2026, 10, 1), date(2026, 10, 1)),  # empty
        (date(2026, 10, 2), date(2026, 10, 1)),  # reversed
        (date(2025, 1, 1), date(2026, 10, 1)),   # too long
    ],
)
def test_invalid_windows_rejected(start, end):
    with pytest.raises(PeriodError):
        validate_window(start, end)


def test_parse_date_rejects_garbage():
    with pytest.raises(PeriodError):
        parse_date("2026-13-01")


def test_second_lock_is_refused(tmp_path):
    with RunLock(tmp_path / ".run.lock"):
        with pytest.raises(AlreadyRunning):
            with RunLock(tmp_path / ".run.lock"):
                pass
    with RunLock(tmp_path / ".run.lock"):  # released
        pass
