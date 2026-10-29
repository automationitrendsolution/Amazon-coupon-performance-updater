"""The command line end to end, with Amazon and MySQL faked."""
from __future__ import annotations

import pytest

import run
from app import spapi
from tests.test_spapi import FakeAmazon


@pytest.fixture
def amazon(monkeypatch, spapi_env, coupon_factory, report_factory):
    fake = FakeAmazon(report_factory(coupon_factory("C-1", asins=("B0A", "B0B"))))
    original = spapi.SpApiClient.__init__

    def init(self, settings, session=None, sleep=None):
        original(self, settings, session=fake, sleep=lambda s: None)

    monkeypatch.setattr(spapi.SpApiClient, "__init__", init)
    return fake


def test_daily_without_apply_is_a_dry_run(amazon, fake_table, project_dirs, capsys):
    assert run.main(["--daily"]) == 0
    assert fake_table.rows == []
    out = capsys.readouterr().out
    assert "dry run: nothing written" in out
    assert list((project_dirs / "reports").glob("coupons_*_records.csv"))


def test_daily_apply_writes_once(amazon, fake_table, project_dirs, tmp_path):
    assert run.main(["--daily", "--apply", "--quiet"]) == 0
    assert len(fake_table.rows) == 2
    calls = len(amazon.calls)

    assert run.main(["--daily", "--apply", "--quiet"]) == 0  # the backup run: already written
    assert len(amazon.calls) == calls and len(fake_table.rows) == 2
    history = (tmp_path / "logs" / "cron.log").read_text(encoding="utf-8").splitlines()
    assert "inserted=2" in history[0] and "already written" in history[1]


def test_window_needs_an_action(amazon):
    with pytest.raises(SystemExit):
        run.main(["--window", "2026-09-01", "2026-10-01"])


def test_bad_window_exit_5(amazon, fake_table):
    assert run.main(["--window", "2026-10-01", "2026-09-01", "--dry-run"]) == 5


def test_missing_settings_exit_2(project_dirs):
    assert run.main(["--daily"]) == 2
