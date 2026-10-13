"""Tests for the Star tracking operational review summary."""

import json
from datetime import date

import review_star_tracking as review


def _snapshot(day, repos, *, duration=5, remaining=4900):
    succeeded = sum(repo["status"] == "ok" for repo in repos)
    failed = sum(repo["status"] not in ("ok", "not_attempted") for repo in repos)
    not_attempted = sum(repo["status"] == "not_attempted" for repo in repos)
    return {
        "schema_version": 1,
        "date": day,
        "source": "github-repos-api",
        "candidate_policy": "official-trending-last-seen-30d",
        "candidate_count": len(repos),
        "attempted": succeeded + failed,
        "http_requests": succeeded + failed + 1,
        "succeeded": succeeded,
        "failed": failed,
        "not_attempted": not_attempted,
        "duration_seconds": duration,
        "rate_limit_remaining": remaining,
        "repos": repos,
    }


def _write_snapshot(data_dir, snapshot):
    path = data_dir / f"{snapshot['date']}.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")


def test_load_window_summarizes_success_failures_quota_and_runtime(tmp_path):
    _write_snapshot(tmp_path, _snapshot("2026-10-01", [
        {"name": "a/one", "status": "ok", "stars": 5},
        {"name": "b/two", "status": "rate_limited", "stars": None},
    ], duration=8, remaining=0))
    _write_snapshot(tmp_path, _snapshot("2026-10-02", [
        {"name": "a/one", "status": "ok", "stars": 6},
    ], duration=4, remaining=4900))

    report = review.load_window(tmp_path, date(2026, 10, 2), 2)

    assert report["window"]["complete"] is True
    assert report["window"]["snapshot_days"] == 2
    assert report["window"]["missing_dates"] == []
    assert report["window"]["incomplete_dates"] == ["2026-10-01"]
    assert report["totals"] == {
        "candidate_count": 3,
        "attempted": 3,
        "http_requests": 5,
        "succeeded": 2,
        "failed": 1,
        "not_attempted": 0,
        "success_rate": 2 / 3,
        "candidate_success_rate": 2 / 3,
    }
    assert report["runtime_seconds"] == {
        "observed_days": 2, "total": 12, "average": 6, "maximum": 8
    }
    assert report["rate_limit_remaining"] == {
        "observed_days": 2, "minimum": 0, "latest": 4900
    }


def test_load_window_reports_missing_and_invalid_days_without_zero_filling(tmp_path):
    _write_snapshot(tmp_path, _snapshot("2026-10-01", [
        {"name": "a/one", "status": "not_attempted", "stars": None},
    ], duration=0, remaining=None))
    (tmp_path / "2026-10-02.json").write_text("{broken", encoding="utf-8")

    report = review.load_window(tmp_path, date(2026, 10, 3), 3)

    assert report["window"]["complete"] is False
    assert report["window"]["snapshot_days"] == 1
    assert report["window"]["missing_dates"] == ["2026-10-03"]
    assert report["window"]["invalid_snapshots"][0]["date"] == "2026-10-02"
    assert report["totals"]["success_rate"] is None
    assert report["runtime_seconds"]["average"] == 0
    assert report["rate_limit_remaining"]["minimum"] is None


def test_load_window_rejects_unknown_repository_status(tmp_path):
    _write_snapshot(tmp_path, _snapshot("2026-10-01", [
        {"name": "a/one", "status": "unexpected", "stars": None},
    ]))

    report = review.load_window(tmp_path, date(2026, 10, 1), 1)

    assert report["window"]["complete"] is False
    assert report["window"]["invalid_snapshots"][0]["reason"]


def test_load_window_requires_positive_window(tmp_path):
    try:
        review.load_window(tmp_path, date(2026, 10, 1), 0)
    except ValueError as exc:
        assert "at least 1" in str(exc)
    else:
        raise AssertionError("Expected zero-day review window to fail")
