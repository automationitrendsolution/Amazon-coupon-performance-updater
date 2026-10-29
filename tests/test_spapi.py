"""SP-API flow with a fake session: create, poll, download, resume, failed reports."""
from __future__ import annotations

import gzip
import json
from datetime import date

import pytest

from app import spapi
from app.config import load_settings
from app.schedule import Window

WINDOW = Window(date(2026, 9, 29), date(2026, 11, 8))


class FakeResponse:
    def __init__(self, status=200, body=None, content=None):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else json.dumps(body or {}).encode()
        self.text = self.content.decode("utf-8", "replace")
        self.headers = {}

    def json(self):
        return self._body


class FakeAmazon:
    """Answers the token, reports and documents calls; ``statuses`` is what each poll returns."""

    def __init__(self, payload, statuses=("IN_QUEUE", "DONE")):
        self.payload = payload
        self.statuses = list(statuses)
        self.created = []
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url))
        if url == spapi.LWA_TOKEN_URL:
            return FakeResponse(body={"access_token": "token", "expires_in": 3600})
        if method == "POST" and url.endswith("/reports"):
            self.created.append(json.loads(kwargs["data"]))
            return FakeResponse(202, {"reportId": f"R{len(self.created)}"})
        if "/documents/" in url:
            return FakeResponse(body={"url": "https://s3.example/doc", "compressionAlgorithm": "GZIP"})
        if "/reports/" in url:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return FakeResponse(body={"processingStatus": status, "reportDocumentId": "D1"})
        if url == "https://s3.example/doc":
            return FakeResponse(content=gzip.compress(json.dumps(self.payload).encode()))
        raise AssertionError(f"unexpected call {method} {url}")


@pytest.fixture
def settings(spapi_env):
    return load_settings()


def test_create_poll_download(settings, tmp_path, coupon_factory, report_factory):
    amazon = FakeAmazon(report_factory(coupon_factory()))
    client = spapi.SpApiClient(settings, session=amazon, sleep=lambda s: None)
    state = tmp_path / "state.json"

    result = client.fetch_coupons(WINDOW, save_dir=tmp_path, state_file=state)

    assert amazon.created == [{"reportType": "GET_COUPON_PERFORMANCE_REPORT", "marketplaceIds": ["ATVPDKIKX0DER"],
                               "reportOptions": {"campaignStartDateFrom": "2026-09-29T00:00:00Z",
                                                 "campaignStartDateTo": "2026-11-08T00:00:00Z"}}]
    assert result.payload["coupons"][0]["couponId"] == "C-1"
    assert result.raw_path.name == "coupons_2026-09-29_2026-11-08_R1.json"
    saved = spapi.load_state(state)
    assert saved["window"] == str(WINDOW) and saved["report"]["raw"] == result.raw_path.name


def test_second_run_uses_the_download(settings, tmp_path, coupon_factory, report_factory):
    amazon = FakeAmazon(report_factory(coupon_factory()))
    client = spapi.SpApiClient(settings, session=amazon, sleep=lambda s: None)
    state = tmp_path / "state.json"
    client.fetch_coupons(WINDOW, save_dir=tmp_path, state_file=state)
    calls = len(amazon.calls)
    again = client.fetch_coupons(WINDOW, save_dir=tmp_path, state_file=state)
    assert len(amazon.calls) == calls and again.report_id == "R1"


def test_timed_out_report_is_resumed(settings, tmp_path, coupon_factory, report_factory, monkeypatch):
    amazon = FakeAmazon(report_factory(coupon_factory()), statuses=["IN_PROGRESS"])
    monkeypatch.setenv("COUPON_POLL_TIMEOUT_SECONDS", "0")
    client = spapi.SpApiClient(load_settings(), session=amazon, sleep=lambda s: None)
    state = tmp_path / "state.json"
    with pytest.raises(spapi.ReportTimeout):
        client.fetch_coupons(WINDOW, save_dir=tmp_path, state_file=state)

    amazon.statuses = ["DONE"]
    result = client.fetch_coupons(WINDOW, save_dir=tmp_path, state_file=state)
    assert len(amazon.created) == 1 and result.report_id == "R1"


def test_failed_report_is_forgotten(settings, tmp_path, coupon_factory, report_factory):
    amazon = FakeAmazon({"error": "bad window"}, statuses=["FATAL"])
    client = spapi.SpApiClient(settings, session=amazon, sleep=lambda s: None)
    state = tmp_path / "state.json"
    with pytest.raises(spapi.ReportFailed, match="bad window"):
        client.fetch_coupons(WINDOW, save_dir=tmp_path, state_file=state)
    assert "report" not in spapi.load_state(state)


def test_missing_credentials_refused(project_dirs):
    from app.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError, match="SPAPI_LWA_CLIENT_ID"):
        spapi.SpApiClient(load_settings())
