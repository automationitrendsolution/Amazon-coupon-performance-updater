"""Writing to api_coupon_data: the report's coupons replaced, other coupons untouched, rollback on failure."""
from __future__ import annotations

import pytest

from app import db
from app.config import load_settings
from app.exceptions import DatabaseError
from app.report import parse


@pytest.fixture
def settings(spapi_env):
    return load_settings()


def test_replaces_the_reports_coupons_only(settings, fake_table, coupon_factory, report_factory):
    fake_table.add(couponId="C-1", asin="B0OLD", clips=1)
    fake_table.add(couponId="C-9", asin="B0KEEP", clips=7)
    report = parse(report_factory(coupon_factory("C-1", asins=("B0A", "B0B")), coupon_factory("C-2")), None)

    plan = db.write(settings, report)

    assert (plan.coupons_in_table, plan.rows_now, plan.rows_new) == (1, 1, 3)
    assert sorted((r["couponId"], r["asin"]) for r in fake_table.rows) == [
        ("C-1", "B0A"), ("C-1", "B0B"), ("C-2", "B0TESTAAAA"), ("C-9", "B0KEEP")]
    assert fake_table.committed == 1


def test_running_twice_gives_no_duplicates(settings, fake_table, coupon_factory, report_factory):
    report = parse(report_factory(coupon_factory("C-1", asins=("B0A", "B0B"))), None)
    db.write(settings, report)
    db.write(settings, report)
    assert len(fake_table.rows) == 2


def test_coupon_without_asins_keeps_its_rows(settings, fake_table, coupon_factory, report_factory):
    fake_table.add(couponId="C-2", asin="B0OLD")
    db.write(settings, parse(report_factory(coupon_factory("C-1"), coupon_factory("C-2", asins=())), None))
    assert ("C-2", "B0OLD") in [(r["couponId"], r["asin"]) for r in fake_table.rows]


def test_report_fields_without_a_column_are_left_out(settings, fake_table, coupon_factory, report_factory):
    coupon = coupon_factory(asins=())
    coupon["asins"] = [{"asin": "B0A", "newField": 5}]
    plan = db.write(settings, parse(report_factory(coupon), None))
    assert plan.not_in_table == ["newField"]
    assert "newField" not in fake_table.rows[0]


def test_failed_write_rolls_back(settings, fake_table, coupon_factory, report_factory):
    fake_table.add(couponId="C-1", asin="B0OLD")
    fake_table.fail_on = "INSERT"
    with pytest.raises(DatabaseError, match="rolled back"):
        db.write(settings, parse(report_factory(coupon_factory("C-1")), None))
    assert [(r["couponId"], r["asin"]) for r in fake_table.rows] == [("C-1", "B0OLD")]


def test_plan_is_read_only(settings, fake_table, coupon_factory, report_factory):
    fake_table.add(couponId="C-1", asin="B0OLD")
    plan = db.plan_write(settings, parse(report_factory(coupon_factory("C-1")), None))
    assert (plan.rows_now, plan.rows_new) == (1, 1)
    assert fake_table.read_only and len(fake_table.rows) == 1


def test_table_without_coupon_id_refused(settings, fake_table, coupon_factory, report_factory):
    fake_table.columns = ["asin", "name"]
    with pytest.raises(DatabaseError, match="couponId"):
        db.plan_write(settings, parse(report_factory(coupon_factory()), None))


def test_inspection(settings, fake_table):
    fake_table.add(couponId="C-1", asin="B0A", startDateTime="2026-10-01T00:00:00Z")
    code, lines = db.inspection(settings)
    assert code == 0
    assert "1 rows, 1 coupons" in "\n".join(lines)
