"""The Coupon Performance report JSON as table rows.

Amazon sends ``{"coupons": [{couponId, name, ..., "asins": [{asin, ...}, ...]}, ...]}``.
Each coupon becomes one row per ASIN: the ASIN's fields first, then the coupon's fields
(the same columns ``pandas.json_normalize(coupons, record_path="asins", meta=COUPON_FIELDS)``
gives, which is how api_coupon_data was first filled). A coupon without ASINs has no rows.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .exceptions import MalformedReport

COUPON_FIELDS = [
    "couponId", "merchantId", "marketplaceId", "currencyCode",
    "name", "websiteMessage", "startDateTime", "endDateTime",
    "discountType", "discountAmount", "totalDiscount", "clips",
    "redemptions", "budget", "budgetSpent", "budgetRemaining",
    "budgetPercentageUsed", "sales",
]
KEY = "couponId"


@dataclass
class CouponReport:
    rows: list[dict] = field(default_factory=list)
    coupons: int = 0
    coupon_ids: list[str] = field(default_factory=list)  # every coupon in the report, also those without ASINs
    without_asins: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def replaced_ids(self) -> list[str]:
        """The coupons written: those with rows. A coupon without ASINs keeps its rows in the table."""
        return list(dict.fromkeys(row[KEY] for row in self.rows))

    @property
    def columns(self) -> list[str]:
        seen: dict[str, None] = {}
        for row in self.rows:
            seen.update(dict.fromkeys(row))
        return list(seen)

    def summary(self) -> list[str]:
        asins = {row.get("asin") for row in self.rows if row.get("asin")}
        lines = [f"report: {self.coupons} coupons, {len(self.rows)} coupon/ASIN rows, {len(asins)} ASINs"]
        if self.without_asins:
            lines.append(f"{len(self.without_asins)} coupon(s) without ASINs (no rows): {' '.join(self.without_asins)}")
        return lines


def _flatten(value: dict, prefix: str = "") -> dict[str, Any]:
    """Nested objects as dotted columns ({"a": {"b": 1}} -> {"a.b": 1}); lists as JSON text."""
    out: dict[str, Any] = {}
    for key, item in value.items():
        name = f"{prefix}{key}"
        if isinstance(item, dict):
            out.update(_flatten(item, f"{name}."))
        elif isinstance(item, list):
            out[name] = json.dumps(item, ensure_ascii=False)
        else:
            out[name] = item
    return out


def parse(payload: dict, marketplace_id: str | None) -> CouponReport:
    coupons = payload.get("coupons") if isinstance(payload, dict) else None
    if not isinstance(coupons, list):
        raise MalformedReport("the report has no 'coupons' list")
    result = CouponReport(coupons=len(coupons))
    for position, coupon in enumerate(coupons):
        if not isinstance(coupon, dict) or not str(coupon.get(KEY) or "").strip():
            raise MalformedReport(f"coupon #{position + 1} has no {KEY}")
        coupon_id = str(coupon[KEY]).strip()
        if coupon_id in result.coupon_ids:
            raise MalformedReport(f"coupon {coupon_id} is in the report twice")
        result.coupon_ids.append(coupon_id)
        market = coupon.get("marketplaceId")
        if marketplace_id and market and market != marketplace_id:
            raise MalformedReport(f"coupon {coupon_id} is for marketplace {market}, not {marketplace_id}")
        meta = {name: coupon.get(name) for name in COUPON_FIELDS}
        meta[KEY] = coupon_id
        missing = [name for name in COUPON_FIELDS if name not in coupon]
        if missing:
            result.warnings.append(f"coupon {coupon_id}: no {', '.join(missing)} (stored as NULL)")
        asins = coupon.get("asins") or []
        if not asins:
            result.without_asins.append(coupon_id)
        for item in asins:
            values = _flatten(item) if isinstance(item, dict) else {"asin": item}
            # A field the ASIN and the coupon both have keeps the coupon's value; the ASIN's is kept as asin.<field>.
            values = {(f"asin.{k}" if k in meta else k): v for k, v in values.items()}
            result.rows.append({**values, **meta})
    return result
