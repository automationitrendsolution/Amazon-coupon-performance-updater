"""Shared fixtures: coupon reports shaped like Amazon's JSON, a fake table, isolated project folders."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

COLUMNS = ["asin", "couponId", "merchantId", "marketplaceId", "currencyCode", "name", "websiteMessage",
           "startDateTime", "endDateTime", "discountType", "discountAmount", "totalDiscount", "clips",
           "redemptions", "budget", "budgetSpent", "budgetRemaining", "budgetPercentageUsed", "sales"]


def make_coupon(coupon_id="C-1", asins=("B0TESTAAAA",), marketplace="ATVPDKIKX0DER", **overrides):
    """A coupons[] entry shaped like Amazon's coupon performance report."""
    coupon = {
        "couponId": coupon_id, "merchantId": "AMERCHANT", "marketplaceId": marketplace, "currencyCode": "USD",
        "name": f"Coupon {coupon_id}", "websiteMessage": "Save 10%", "startDateTime": "2026-10-01T00:00:00Z",
        "endDateTime": "2026-10-31T23:59:59Z", "discountType": "PERCENT_OFF_LIST_PRICE", "discountAmount": 10,
        "totalDiscount": 25.5, "clips": 120, "redemptions": 14, "budget": 500, "budgetSpent": 25.5,
        "budgetRemaining": 474.5, "budgetPercentageUsed": 5.1, "sales": 340.0,
        "asins": [{"asin": asin} for asin in asins],
    }
    coupon.update(overrides)
    return coupon


def make_report(*coupons):
    return {"coupons": list(coupons)}


@pytest.fixture
def coupon_factory():
    return make_coupon


@pytest.fixture
def report_factory():
    return make_report


class FakeTable:
    """powerbidb.api_coupon_data in memory, answering the statements app/db.py sends.

    Read-only connections refuse writes like MySQL does; ``fail_on`` makes a statement
    starting with that text raise, to test the rollback.
    """

    def __init__(self, columns=COLUMNS):
        self.columns = list(columns)
        self.rows: list[dict] = []
        self.read_only = True
        self.fail_on: str | None = None
        self.committed = 0
        self._snapshot = None

    def add(self, **row):
        self.rows.append({c: row.get(c) for c in self.columns})

    # connection
    def cursor(self):
        return FakeTableCursor(self)

    def begin(self):
        self._snapshot = [dict(r) for r in self.rows]

    def commit(self):
        self._snapshot = None
        self.committed += 1

    def rollback(self):
        if self._snapshot is not None:
            self.rows = [dict(r) for r in self._snapshot]
        self._snapshot = None


class FakeTableCursor:
    def __init__(self, table: FakeTable):
        self.table, self.result = table, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _check(self, sql):
        import pymysql

        if self.table.fail_on and sql.startswith(self.table.fail_on):
            raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
        if self.table.read_only and sql.startswith(("UPDATE", "INSERT", "DELETE")):
            raise pymysql.err.OperationalError(1792, "Cannot execute statement in a READ ONLY transaction.")

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self._check(sql)
        rows = self.table.rows
        matching = [r for r in rows if r["couponId"] in params] if "IN (" in sql else rows
        if sql.startswith("SELECT COLUMN_NAME FROM information_schema"):
            self.result = [{"COLUMN_NAME": c} for c in self.table.columns]
        elif sql.startswith("SELECT COLUMN_NAME, COLUMN_TYPE"):
            self.result = [{"COLUMN_NAME": c, "COLUMN_TYPE": "text", "IS_NULLABLE": "YES", "COLUMN_KEY": "",
                            "EXTRA": ""} for c in self.table.columns]
        elif sql.startswith("SELECT COUNT(*) AS n, COUNT(DISTINCT"):
            self.result = [{"n": len(matching), "coupons": len({r["couponId"] for r in matching})}]
        elif sql.startswith("SELECT COUNT(*) AS n FROM"):
            self.result = [{"n": len(matching)}]
        elif sql.startswith("SELECT MIN(startDateTime)"):
            starts = sorted(r["startDateTime"] for r in rows if r["startDateTime"])
            self.result = [{"first": starts[0] if starts else None, "last": starts[-1] if starts else None}]
        elif sql.startswith("DELETE FROM"):
            self.table.rows = [r for r in rows if r["couponId"] not in params]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def executemany(self, sql, seq):
        import re

        sql = " ".join(sql.split())
        self._check(sql)
        assert sql.startswith("INSERT"), sql
        columns = re.findall(r"`(\w+)`", sql.split(" VALUES ")[0])[1:]  # [0] is the table
        for values in seq:
            self.table.add(**dict(zip(columns, values)))

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0]


@pytest.fixture
def fake_table(monkeypatch):
    """Replace the MySQL connection with a FakeTable (the table starts empty)."""
    from contextlib import contextmanager

    from app import db

    table = FakeTable()

    @contextmanager
    def connect(settings, read_only=True):
        table.read_only = read_only
        yield table

    monkeypatch.setattr(db, "connect", connect)
    return table


@pytest.fixture(autouse=True)
def project_dirs(tmp_path, monkeypatch):
    """Every test gets its own data/ and logs/ and no real .env values."""
    data = tmp_path / "data"
    monkeypatch.setenv("COUPON_DATA_DIR", str(data))
    monkeypatch.setenv("COUPON_LOG_DIR", str(tmp_path / "logs"))
    for name in ("SPAPI_LWA_CLIENT_ID", "SPAPI_LWA_CLIENT_SECRET", "SPAPI_LWA_REFRESH_TOKEN", "SPAPI_ENDPOINT",
                 "SPAPI_MARKETPLACE_ID", "DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "COUPON_TABLE",
                 "POWERBI_DB_HOST", "POWERBI_DB_USER", "POWERBI_DB_PASSWORD", "POWERBI_DB_NAME",
                 "COUPON_LOOKBACK_DAYS", "COUPON_LOOKAHEAD_DAYS", "LOG_LEVEL"):
        monkeypatch.setenv(name, "")
    return data


@pytest.fixture
def spapi_env(monkeypatch):
    """SP-API and MySQL settings filled with test values."""
    for name, value in (("SPAPI_LWA_CLIENT_ID", "amzn1.application-oa2-client.test"),
                        ("SPAPI_LWA_CLIENT_SECRET", "secret"), ("SPAPI_LWA_REFRESH_TOKEN", "Atzr|test"),
                        ("SPAPI_MARKETPLACE_ID", "ATVPDKIKX0DER"), ("DB_HOST", "db"), ("DB_USER", "u"),
                        ("DB_PASSWORD", "p"), ("COUPON_POLL_INTERVAL_SECONDS", "0")):
        monkeypatch.setenv(name, value)
