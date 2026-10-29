"""The report JSON as rows: one per coupon and ASIN, coupon fields repeated, bad reports refused."""
from __future__ import annotations

import json

import pytest

from app.exceptions import MalformedReport
from app.report import COUPON_FIELDS, parse


def test_one_row_per_coupon_and_asin(coupon_factory, report_factory):
    payload = report_factory(coupon_factory("C-1", asins=("B0A", "B0B")), coupon_factory("C-2", asins=("B0C",)))
    result = parse(payload, "ATVPDKIKX0DER")
    assert result.coupons == 2
    assert [(r["couponId"], r["asin"]) for r in result.rows] == [("C-1", "B0A"), ("C-1", "B0B"), ("C-2", "B0C")]
    assert result.columns == ["asin", *COUPON_FIELDS]  # the order json_normalize gave api_coupon_data
    assert result.rows[0]["clips"] == 120 and result.rows[0]["sales"] == 340.0
    assert result.replaced_ids == ["C-1", "C-2"]


def test_coupon_without_asins_has_no_rows_and_is_not_replaced(coupon_factory, report_factory):
    result = parse(report_factory(coupon_factory("C-1"), coupon_factory("C-2", asins=())), None)
    assert result.without_asins == ["C-2"]
    assert result.coupon_ids == ["C-1", "C-2"]
    assert result.replaced_ids == ["C-1"]


def test_asin_fields_are_flattened_and_never_hide_coupon_fields(coupon_factory, report_factory):
    coupon = coupon_factory(asins=())
    coupon["asins"] = [{"asin": "B0A", "sales": 99, "extra": {"a": 1}, "tags": ["x"]}]
    row = parse(report_factory(coupon), None).rows[0]
    assert row["sales"] == 340.0 and row["asin.sales"] == 99
    assert row["extra.a"] == 1 and json.loads(row["tags"]) == ["x"]


def test_plain_asin_strings(coupon_factory, report_factory):
    coupon = coupon_factory(asins=())
    coupon["asins"] = ["B0A"]
    assert parse(report_factory(coupon), None).rows[0]["asin"] == "B0A"


def test_missing_fields_are_null_with_a_warning(coupon_factory, report_factory):
    coupon = coupon_factory()
    del coupon["budget"]
    result = parse(report_factory(coupon), None)
    assert result.rows[0]["budget"] is None
    assert "budget" in result.warnings[0]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"coupons": "nope"},
        {"coupons": [{"name": "no id"}]},
    ],
)
def test_malformed_reports_refused(payload):
    with pytest.raises(MalformedReport):
        parse(payload, None)


def test_other_marketplace_refused(coupon_factory, report_factory):
    with pytest.raises(MalformedReport, match="marketplace"):
        parse(report_factory(coupon_factory(marketplace="A1F83G8C2ARO7P")), "ATVPDKIKX0DER")


def test_duplicate_coupon_refused(coupon_factory, report_factory):
    with pytest.raises(MalformedReport, match="twice"):
        parse(report_factory(coupon_factory("C-1"), coupon_factory("C-1")), None)
