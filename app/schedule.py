"""The campaign start window of a run, and the single-run lock for the scheduled job.

The coupon report covers coupons whose campaign *starts* inside a window. The daily run
asks for [today - COUPON_LOOKBACK_DAYS, today + COUPON_LOOKAHEAD_DAYS] in UTC, so running,
recently ended and upcoming coupons are all refreshed. A window given by hand is used as is.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

from .exceptions import Locked, PeriodError

MAX_WINDOW_DAYS = 366


@dataclass(frozen=True)
class Window:
    start: dt.date
    end: dt.date

    def amazon(self) -> tuple[str, str]:
        """campaignStartDateFrom / campaignStartDateTo, as Amazon expects them (UTC, ISO 8601)."""
        return f"{self.start.isoformat()}T00:00:00Z", f"{self.end.isoformat()}T00:00:00Z"

    def __str__(self) -> str:
        return f"{self.start}..{self.end}"


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise PeriodError(f"Invalid date {value!r}; expected YYYY-MM-DD") from None


def validate_window(start: dt.date, end: dt.date) -> Window:
    if end <= start:
        raise PeriodError(f"The window must end after it starts ({start}..{end})")
    if (end - start).days > MAX_WINDOW_DAYS:
        raise PeriodError(f"The window is {(end - start).days} days; at most {MAX_WINDOW_DAYS}")
    return Window(start, end)


def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def daily_window(today: dt.date, lookback_days: int, lookahead_days: int) -> Window:
    """Coupons starting from ``lookback_days`` ago up to ``lookahead_days`` ahead."""
    return validate_window(today - dt.timedelta(days=lookback_days), today + dt.timedelta(days=lookahead_days))


class AlreadyRunning(Locked):
    """Another scheduled run holds the lock file."""


class RunLock:
    """An OS-level exclusive lock on a file; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(f"another run holds {self.path}") from None
        self._handle = handle
        return self

    def __exit__(self, *exc) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
