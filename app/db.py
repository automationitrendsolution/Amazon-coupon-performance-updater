"""MySQL table powerbidb.api_coupon_data (DB_*, COUPON_TABLE): inspection, and writing a report.

Read-only sessions make MySQL itself reject any write statement, so ``--inspect`` and
dry runs cannot change the table. Errors reaching MySQL end the run with exit 3.

How a report is stored: one row per coupon and ASIN, columns named like the report's
fields (couponId, asin, name, clips, redemptions, sales, ...). Every coupon in the report
that has ASINs is replaced as a whole: its rows are deleted and the report's rows inserted,
in one transaction. Coupons the report does not have, or has without ASINs, are never touched.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from .config import Settings
from .exceptions import DatabaseError
from .logging_config import get_logger
from .report import KEY, CouponReport

log = get_logger("db")

# Rows deleted per statement, so a large report does not build one huge IN (...) list.
CHUNK = 500


def quote(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


@contextmanager
def connect(settings: Settings, read_only: bool = True) -> Iterator:
    """Open a connection. Read-only sessions make MySQL reject any write statement."""
    import pymysql
    import pymysql.cursors

    settings.require_db()
    try:
        conn = pymysql.connect(host=settings.db_host, port=settings.db_port, user=settings.db_user,
                               password=settings.db_password, database=settings.db_name, charset="utf8mb4",
                               autocommit=False, cursorclass=pymysql.cursors.DictCursor, connect_timeout=15)
    except pymysql.err.OperationalError as exc:
        raise DatabaseError(f"MySQL connection failed: {exc}") from None
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY" if read_only else "SET SESSION TRANSACTION READ WRITE")
        yield conn
    except pymysql.err.OperationalError as exc:
        raise DatabaseError(f"MySQL connection failed: {exc}") from None
    finally:
        conn.close()


def table_columns(cur, settings: Settings) -> list[str]:
    cur.execute(
        """
        SELECT COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (settings.db_name, settings.db_table),
    )
    columns = [row["COLUMN_NAME"] for row in cur.fetchall()]
    if not columns:
        raise DatabaseError(f"Table {settings.db_name}.{settings.db_table} not found or not visible to this user")
    if KEY not in columns:
        raise DatabaseError(f"Table {settings.db_table} has no {KEY} column")
    return columns


def inspection(settings: Settings) -> tuple[int, list[str]]:
    """Read-only report on the table: columns, row and coupon counts, newest campaigns. Changes nothing."""
    table = quote(settings.db_table)
    with connect(settings, read_only=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, EXTRA
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (settings.db_name, settings.db_table),
        )
        columns = list(cur.fetchall())
        if not columns:
            raise DatabaseError(f"Table {settings.db_name}.{settings.db_table} not found or not visible to this user")
        names = [c["COLUMN_NAME"] for c in columns]
        lines = [f"\nTable {settings.db_name}.{settings.db_table}\n", f"{'column':40} {'type':28} null key extra"]
        lines += [f"{c['COLUMN_NAME']:40} {c['COLUMN_TYPE']:28} {c['IS_NULLABLE']:4} {c['COLUMN_KEY']:3} {c['EXTRA']}"
                  for c in columns]
        if KEY not in names:
            lines.append(f"\nNo {KEY} column: this table cannot be written by the updater.")
            conn.rollback()
            return 1, lines
        cur.execute(f"SELECT COUNT(*) AS n, COUNT(DISTINCT {quote(KEY)}) AS coupons FROM {table}")
        counts = cur.fetchone()
        lines.append(f"\n{counts['n']} rows, {counts['coupons']} coupons")
        if "startDateTime" in names:
            cur.execute(f"SELECT MIN(startDateTime) AS first, MAX(startDateTime) AS last FROM {table}")
            span = cur.fetchone()
            lines.append(f"campaign starts: {span['first']} .. {span['last']}")
        conn.rollback()
    return 0, lines


@dataclass
class WritePlan:
    columns: list[str]  # report columns written
    not_in_table: list[str] = field(default_factory=list)  # report columns the table lacks: left out
    coupons_in_table: int = 0  # of the report's coupons, how many the table has now
    rows_now: int = 0  # the table's rows of those coupons now
    rows_new: int = 0

    def summary(self) -> list[str]:
        lines = [f"plan: replace {self.coupons_in_table} coupon(s) already in the table ({self.rows_now} rows) "
                 f"and add the others: {self.rows_now} rows deleted, {self.rows_new} inserted"]
        if self.not_in_table:
            lines.append(f"report fields with no table column (left out): {', '.join(self.not_in_table)}")
        return lines


def _chunks(items: list[str]) -> Iterator[list[str]]:
    for i in range(0, len(items), CHUNK):
        yield items[i:i + CHUNK]


def _plan(cur, settings: Settings, report: CouponReport) -> WritePlan:
    available = table_columns(cur, settings)
    plan = WritePlan(columns=[c for c in report.columns if c in available],
                     not_in_table=[c for c in report.columns if c not in available], rows_new=len(report.rows))
    table = quote(settings.db_table)
    for ids in _chunks(report.replaced_ids):
        marks = ", ".join(["%s"] * len(ids))
        cur.execute(f"SELECT COUNT(*) AS n, COUNT(DISTINCT {quote(KEY)}) AS coupons FROM {table} "
                    f"WHERE {quote(KEY)} IN ({marks})", ids)
        found = cur.fetchone()
        plan.rows_now += found["n"]
        plan.coupons_in_table += found["coupons"]
    return plan


def plan_write(settings: Settings, report: CouponReport) -> WritePlan:
    """Read-only: what writing the report would delete and insert. Changes nothing."""
    with connect(settings, read_only=True) as conn:
        with conn.cursor() as cur:
            plan = _plan(cur, settings, report)
        conn.rollback()
    return plan


def write(settings: Settings, report: CouponReport) -> WritePlan:
    """Replace the report's coupons in one transaction; anything unexpected rolls it all back."""
    import pymysql

    table = quote(settings.db_table)
    with connect(settings, read_only=False) as conn:
        try:
            conn.begin()
            with conn.cursor() as cur:
                plan = _plan(cur, settings, report)
                for ids in _chunks(report.replaced_ids):
                    cur.execute(f"DELETE FROM {table} WHERE {quote(KEY)} IN ({', '.join(['%s'] * len(ids))})", ids)
                if report.rows:
                    cur.executemany(f"INSERT INTO {table} ({', '.join(quote(c) for c in plan.columns)}) "
                                    f"VALUES ({', '.join(['%s'] * len(plan.columns))})",
                                    [tuple(row.get(c) for c in plan.columns) for row in report.rows])
                count = 0
                for ids in _chunks(report.replaced_ids):
                    cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {quote(KEY)} IN "
                                f"({', '.join(['%s'] * len(ids))})", ids)
                    count += cur.fetchone()["n"]
                if count != len(report.rows):
                    raise DatabaseError(f"expected {len(report.rows)} rows of the report's coupons after writing, "
                                        f"found {count}")
            conn.commit()
        except (pymysql.MySQLError, DatabaseError) as exc:
            try:
                conn.rollback()
            except pymysql.MySQLError:
                pass  # the connection is gone: MySQL discards the open transaction itself
            raise DatabaseError(f"writing {settings.db_table}: {exc}; rolled back, nothing changed") from None
    log.info("%s.%s: %d coupons, deleted %d rows, inserted %d", settings.db_name, settings.db_table,
             len(report.replaced_ids), plan.rows_now, plan.rows_new)
    return plan
