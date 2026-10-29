"""SP-API client for the Coupon Performance report.

Authentication is LWA-only (refresh token -> access token); SP-API no longer
requires AWS SigV4 signing. Reports API flow: createReport -> poll getReport
-> getReportDocument -> download (gzip) JSON, saved to data/reports/.
"""
from __future__ import annotations

import gzip
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .config import REPORT_TYPE, Settings
from .exceptions import SpApiError
from .logging_config import get_logger
from .schedule import Window

log = get_logger("spapi")

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
REPORTS_PATH = "/reports/2021-06-30"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = "amazon-coupon-performance-updater/1.0 (Language=Python)"


class ReportFailed(SpApiError):
    """Amazon finished the report with CANCELLED or FATAL status."""

    def __init__(self, report_id: str, status: str, detail: str | None = None):
        self.report_id = report_id
        self.status = status
        self.detail = detail
        msg = f"Report {report_id} ended with status {status}"
        super().__init__(f"{msg}: {detail}" if detail else msg)


class ReportTimeout(SpApiError):
    """Amazon did not finish the report within COUPON_POLL_TIMEOUT_SECONDS."""


@dataclass
class ReportResult:
    report_id: str
    document_id: str
    payload: dict
    raw_path: Path | None


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 300.0, rate_limit: float | None = None) -> float:
    delay = min(cap, base * (2**attempt)) * (0.5 + random.random() / 2)
    if rate_limit and rate_limit > 0:
        delay = max(delay, 1.0 / rate_limit)
    return delay


class SpApiClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        settings.require_spapi()
        self.settings = settings
        self.session = session or requests.Session()
        self.sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    # ----------------------------------------------------------------------- auth

    def _access_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expires - 60:
            return self._token
        resp = self._send_with_retry(
            "POST",
            LWA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.settings.lwa_refresh_token,
                "client_id": self.settings.lwa_client_id,
                "client_secret": self.settings.lwa_client_secret,
            },
        )
        if resp.status_code != 200:
            raise SpApiError(f"LWA token request failed ({resp.status_code}): {resp.text[:500]}")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600))
        return self._token

    # ----------------------------------------------------------------------- transport

    def _send_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                resp = self.session.request(method, url, timeout=60, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
                delay = backoff_delay(attempt)
                log.warning("%s %s failed (%s); retry %d in %.0fs", method, url, exc, attempt + 1, delay)
                self.sleep(delay)
                continue
            if resp.status_code in RETRYABLE_STATUS and attempt < self.settings.max_retries:
                rate = resp.headers.get("x-amzn-RateLimit-Limit")
                delay = backoff_delay(attempt, rate_limit=float(rate) if rate else None)
                log.warning("%s %s -> HTTP %d (request id %s); retry %d in %.0fs", method, url, resp.status_code,
                            resp.headers.get("x-amzn-RequestId"), attempt + 1, delay)
                self.sleep(delay)
                continue
            return resp
        raise SpApiError(f"{method} {url} failed after retries: {last_error}")

    def _api(self, method: str, path: str, **kwargs) -> dict:
        url = self.settings.sp_api_endpoint + path
        for refreshed in (False, True):
            headers = {
                "x-amz-access-token": self._access_token(force=refreshed),
                "user-agent": USER_AGENT,
                "content-type": "application/json",
            }
            resp = self._send_with_retry(method, url, headers=headers, **kwargs)
            if resp.status_code in (401, 403) and not refreshed and "expired" in resp.text.lower():
                continue
            break
        if resp.status_code >= 400:
            hint = ""
            if resp.status_code == 403:
                hint = " (check the app's roles cover coupon reports and the refresh token is for this account)"
            raise SpApiError(f"{method} {path} -> HTTP {resp.status_code}{hint}: {resp.text[:1000]}")
        return resp.json() if resp.content else {}

    # ----------------------------------------------------------------------- reports

    def create_report(self, window: Window) -> str:
        start, end = window.amazon()
        body = {
            "reportType": REPORT_TYPE,
            "marketplaceIds": [self.settings.marketplace_id],
            "reportOptions": {"campaignStartDateFrom": start, "campaignStartDateTo": end},
        }
        result = self._api("POST", f"{REPORTS_PATH}/reports", data=json.dumps(body))
        report_id = result["reportId"]
        log.info("Created report %s for coupons starting %s", report_id, window)
        return report_id

    def wait_for_report(self, report_id: str) -> str:
        """Poll until DONE and return the reportDocumentId."""
        deadline = time.monotonic() + self.settings.poll_timeout_seconds
        while True:
            report = self._api("GET", f"{REPORTS_PATH}/reports/{report_id}")
            status = report.get("processingStatus")
            log.info("Report %s status %s", report_id, status)
            if status == "DONE":
                return report["reportDocumentId"]
            if status in ("CANCELLED", "FATAL"):
                detail = None
                if report.get("reportDocumentId"):
                    # FATAL reports usually carry an error document explaining why.
                    try:
                        detail = self.download_document(report["reportDocumentId"]).decode("utf-8", "replace")[:2000]
                    except SpApiError as exc:
                        detail = f"(could not fetch error document: {exc})"
                raise ReportFailed(report_id, status, detail)
            if time.monotonic() >= deadline:
                raise ReportTimeout(f"Report {report_id} still {status} after {self.settings.poll_timeout_seconds}s")
            self.sleep(self.settings.poll_interval_seconds)

    def download_document(self, document_id: str) -> bytes:
        meta = self._api("GET", f"{REPORTS_PATH}/documents/{document_id}")
        # Pre-signed S3 URL: must not carry SP-API headers.
        resp = self._send_with_retry("GET", meta["url"])
        if resp.status_code != 200:
            raise SpApiError(f"Document {document_id} download failed: HTTP {resp.status_code}")
        data = resp.content
        if meta.get("compressionAlgorithm") == "GZIP":
            data = gzip.decompress(data)
        return data

    def fetch_coupons(self, window: Window, save_dir: Path | None = None, state_file: Path | None = None,
                      refetch: bool = False) -> ReportResult:
        """Request, wait for and download the report of ``window``.

        With ``state_file`` the report ID is recorded as soon as it is created: a run that is
        cut off or times out is carried on by the next run with the same report, and a report
        already downloaded to ``save_dir`` is read from there. ``refetch`` asks Amazon again.
        """
        state = {} if refetch else load_state(state_file)
        entry = state.get("report") if state.get("window") == str(window) else None
        entry = dict(entry or {})
        if entry.get("raw") and save_dir and (save_dir / entry["raw"]).is_file():
            path = save_dir / entry["raw"]
            log.info("Using downloaded report %s (%s)", entry["report_id"], path.name)
            return ReportResult(entry["report_id"], entry.get("document_id", ""), _json(path.read_bytes(),
                                                                                          entry["report_id"]), path)
        report_id = entry.get("report_id")
        if report_id:
            log.info("Resuming report %s for coupons starting %s", report_id, window)
        else:
            report_id = self.create_report(window)
            entry = {"report_id": report_id}
            _save_report(state_file, window, entry)
        try:
            document_id = self.wait_for_report(report_id)
        except ReportFailed:
            _save_report(state_file, window, None)  # the next run asks Amazon again
            raise
        raw = self.download_document(document_id)
        raw_path = None
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            raw_path = save_dir / f"coupons_{window.start}_{window.end}_{report_id}.json"
            raw_path.write_bytes(raw)
        payload = _json(raw, report_id)
        log.info("Downloaded report %s (document %s, %d bytes)", report_id, document_id, len(raw))
        entry.update(document_id=document_id, raw=raw_path.name if raw_path else None)
        _save_report(state_file, window, entry)
        return ReportResult(report_id, document_id, payload, raw_path)


def _json(raw: bytes, report_id: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpApiError(f"Report {report_id} document is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------- run state


def state_file(state_dir: Path, day) -> Path:
    """One state file per day: the day's runs (main and backup) share the report."""
    return state_dir / f"coupons_{day}.json"


def load_state(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpApiError(f"cannot read run state {path}: {exc}; delete it to start the day again") from None


def save_state(path: Path | None, state: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _save_report(path: Path | None, window: Window, entry: dict | None) -> None:
    """Record the report in the day's state file, keeping whatever else is in it (another run may have
    marked the day written since this run read it). None forgets the report."""
    if path is None:
        return
    state = load_state(path)
    if entry is None:
        state.pop("report", None)
    else:
        state["window"], state["report"] = str(window), dict(entry)
    save_state(path, state)
