#!/usr/bin/env python3
"""Amazon coupon performance updater -- "Coupon Performance" report, command-line entry point.

Requests Amazon's coupon performance report (SP-API, report type
GET_COUPON_PERFORMANCE_REPORT) for the coupons whose campaign starts in a window, checks it,
and writes it to powerbidb.api_coupon_data: one row per coupon and ASIN. Every coupon in the
report is replaced as a whole (its rows deleted, the report's inserted) in one transaction;
other coupons are never touched. ``--inspect`` and dry runs read the table only (read-only session).

The report ID is kept in data/state/, so a run that is cut off or times out (exit 6) carries on
with the same report next time, and a report already downloaded to data/reports/ is not
requested again the same day.

Running under cron
------------------
The scheduler (Windows Task Scheduler, deploy/install_task.ps1, or cron, deploy/crontab.example)
calls ``run.py --daily --apply --quiet`` every day at 09:00 IST, with a backup run at 15:00 IST for
when Amazon was slow. The first run that completes writes the day; the backup finds it written and
stops at once. Overlapping runs are refused by an internal lock (exit 11).

Exit codes
----------
    0   success
    1   unexpected error (see logs/coupon_updater.log)
    2   configuration error
    3   MySQL failed, or a write was rolled back (nothing changed)
    5   the window is not valid
    6   SP-API request failed, or the report was not ready in time (the next run resumes it)
    7   the report was for another marketplace, or not shaped as expected
    11  another run holds the lock
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ folders in the project; set before app/ is imported

from app import __version__, db, report, spapi
from app.config import Settings, load_settings
from app.exceptions import ConfigurationError, PipelineError
from app.logging_config import append_run, configure_logging, get_logger
from app.schedule import RunLock, Window, daily_window, parse_date, today_utc, validate_window

_HELP_EPILOG = """
examples:
  python run.py --inspect                                 read-only check of the table
  python run.py --daily                                   fetch, validate and preview today's window; never writes
  python run.py --daily --apply                           write it to MySQL (cron)
  python run.py --window 2026-09-01 2026-10-01 --dry-run  coupons starting in that window, preview only
  python run.py --window 2026-09-01 2026-10-01 --apply    write them
  python run.py --daily --apply --refetch                 ask Amazon again (ignore today's download)

exit codes:
  0 success · 1 unexpected · 2 config · 3 MySQL · 5 bad window · 6 SP-API failed · 7 malformed report · 11 locked
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amazon-coupon-performance-updater",
        description="Fetch Amazon coupon performance and replace those coupons in MySQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"amazon-coupon-performance-updater {__version__}")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--inspect", action="store_true", help="Inspect the table (read-only).")
    modes.add_argument("--daily", action="store_true",
                       help="Coupons starting from COUPON_LOOKBACK_DAYS ago to COUPON_LOOKAHEAD_DAYS ahead (cron).")
    modes.add_argument("--window", nargs=2, metavar=("FROM", "TO"),
                       help="Coupons whose campaign starts in FROM..TO, YYYY-MM-DD (UTC).")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="Fetch, validate and preview; never writes.")
    action.add_argument("--apply", action="store_true", help="Write the validated coupons to MySQL.")
    parser.add_argument("--refetch", action="store_true",
                        help="Ask Amazon again: ignore the downloaded report and the pending report ID.")
    parser.add_argument("--env-file", type=Path, metavar="PATH", help="Alternative .env file.")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Override LOG_LEVEL.")
    parser.add_argument("--quiet", action="store_true", help="Console shows warnings and errors only (cron).")
    parser.add_argument("--no-lock", action="store_true", help="Skip the single-run lock (supervised runs only).")
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.inspect and (args.dry_run or args.apply or args.refetch):
        parser.error("--inspect cannot be combined with --dry-run/--apply/--refetch")
    if args.window and not (args.dry_run or args.apply):
        parser.error("--window requires --dry-run or --apply")
    if args.daily and not (args.dry_run or args.apply):
        # Scheduled runs must say --apply explicitly; anything else previews.
        args.dry_run = True


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # the downloaded reports stay private to this user
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    started = dt.datetime.now()
    try:
        settings = load_settings(args.env_file)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return exc.exit_code
    settings.ensure_directories()
    level = args.log_level or settings.log_level
    configure_logging(settings.log_dir, level=level, console_level="WARNING" if args.quiet else level)
    logger = get_logger("cli")

    code, summary = 1, ""
    try:
        code, summary = _run(args, settings, logger)
    except PipelineError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        code, summary = exc.exit_code, f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        code = 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected failure")
        code, summary = 1, f"{type(exc).__name__}: {exc}"
    append_run(settings.log_dir, f"{started:%Y-%m-%d %H:%M:%S} exit={code} {' '.join(argv or sys.argv[1:])} | {summary}")
    return code


def _run(args: argparse.Namespace, settings: Settings, logger) -> tuple[int, str]:
    if args.inspect:
        code, lines = db.inspection(settings)
        print("\n".join(lines))
        return code, "inspected" if code == 0 else f"no {report.KEY} column"

    lock = RunLock(settings.log_dir / ".run.lock") if not args.no_lock else None
    if lock:
        lock.__enter__()
    try:
        return _update(args, settings, logger)
    finally:
        if lock:
            lock.__exit__(None, None, None)


def _window(args: argparse.Namespace, settings: Settings) -> tuple[Window, Path]:
    """The window and its state file: --daily shares one per day, --window one per window."""
    if args.daily:
        today = today_utc()
        return (daily_window(today, settings.lookback_days, settings.lookahead_days),
                spapi.state_file(settings.state_dir, today.isoformat()))
    window = validate_window(parse_date(args.window[0]), parse_date(args.window[1]))
    return window, spapi.state_file(settings.state_dir, f"window_{window.start}_{window.end}")


def _update(args: argparse.Namespace, settings: Settings, logger) -> tuple[int, str]:
    window, state_file = _window(args, settings)
    written = spapi.load_state(state_file).get("written")
    if args.daily and args.apply and written and not args.refetch:
        logger.info("today's window %s was already written at %s; nothing to do", window, written["at"])
        return 0, f"{window} already written"

    logger.info("requesting coupons starting %s, marketplace %s", window, settings.marketplace_id)
    client = spapi.SpApiClient(settings)
    result = client.fetch_coupons(window, save_dir=settings.report_dir, state_file=state_file, refetch=args.refetch)
    parsed = report.parse(result.payload, settings.marketplace_id)
    for line in parsed.summary():
        logger.info("%s", line)
    for warning in parsed.warnings[:50]:
        logger.warning("%s", warning)
    records_file = _save_records(parsed, window, settings)
    print("\n".join(["", f"window {window}, report {result.report_id}", *parsed.summary(), f"records: {records_file}"]))

    summary = f"{window} report={result.report_id} coupons={parsed.coupons} rows={len(parsed.rows)}"
    if not parsed.rows:
        logger.warning("no coupon rows in the report: nothing to write")
        return 0, summary + " nothing to write"

    if args.dry_run:
        plan = db.plan_write(settings, parsed)
        print("\n".join([*plan.summary(), f"dry run: nothing written to {settings.db_name}.{settings.db_table}"]))
        for line in plan.summary():
            logger.info("%s", line)
        return 0, f"{summary} dry-run delete={plan.rows_now} insert={plan.rows_new}"

    plan = db.write(settings, parsed)
    for line in plan.summary():
        logger.info("%s", line)
    state = spapi.load_state(state_file)
    state["written"] = {"at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "report": result.report_id,
                        "coupons": len(parsed.replaced_ids), "deleted": plan.rows_now, "inserted": plan.rows_new}
    spapi.save_state(state_file, state)
    print("\n".join([*plan.summary(), f"written to {settings.db_name}.{settings.db_table}"]))
    return 0, f"{summary} deleted={plan.rows_now} inserted={plan.rows_new}"


def _save_records(parsed: report.CouponReport, window: Window, settings: Settings) -> Path:
    """The report's rows as CSV (what api_coupon_data.csv used to be), for reviewing a dry run."""
    path = settings.report_dir / f"coupons_{window.start}_{window.end}_records.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=parsed.columns or [report.KEY])
        writer.writeheader()
        writer.writerows(parsed.rows)
    return path


if __name__ == "__main__":
    sys.exit(main())
