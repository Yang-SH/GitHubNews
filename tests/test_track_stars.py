"""Tests for official-trending candidate selection and daily Star snapshots."""

import json
from datetime import date

import requests
import pytest

import track_stars as ts
import fetch_trending as ft


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = 0
        self.request_headers = []

    def get(self, url, **kwargs):
        self.calls += 1
        self.request_headers.append(kwargs.get("headers", {}))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(ft, "RETRY_BACKOFF_SECONDS", 0)


def _write_snapshot(path, day, repos, **extra):
    path.write_text(
        json.dumps({"date": day, "daily": [{"name": name} for name in repos], **extra}),
        encoding="utf-8",
    )


def test_load_candidates_filters_window_and_sources(tmp_path):
    _write_snapshot(tmp_path / "2026-10-01.json", "2026-10-01", ["old/repo"])
    _write_snapshot(tmp_path / "2026-10-02.json", "2026-10-02", ["owner/repo"])
    _write_snapshot(
        tmp_path / "2026-10-03.json", "2026-10-03", ["backfill/repo"],
        source="gharchive-watchevent",
    )
    _write_snapshot(tmp_path / "2026-10-04.json", "2026-10-04", ["owner/repo", "new/repo"])

    assert ts.load_candidates(tmp_path, date(2026, 10, 4), window_days=3) == {
        "owner/repo": "2026-10-04",
        "new/repo": "2026-10-04",
    }


def test_load_candidates_rejects_mismatched_snapshot_date(tmp_path):
    _write_snapshot(tmp_path / "2026-10-04.json", "2026-10-03", ["owner/repo"])
    try:
        ts.load_candidates(tmp_path, date(2026, 10, 4))
    except RuntimeError as exc:
        assert "日期与文件名不一致" in str(exc)
    else:
        raise AssertionError("Expected date mismatch to fail")


def test_collect_snapshot_records_success_not_found_and_forbidden():
    session = FakeSession(
        FakeResponse(payload={"stargazers_count": 123}),
        FakeResponse(404),
        FakeResponse(403),
    )
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01", "b/two": "2026-10-02", "c/three": "2026-10-03"},
        "token",
        date(2026, 10, 4),
        session=session,
    )

    assert [(r["status"], r["stars"]) for r in snapshot["repos"]] == [
        ("ok", 123), ("not_found", None), ("forbidden", None)
    ]
    assert (snapshot["candidate_count"], snapshot["attempted"]) == (3, 3)
    assert snapshot["http_requests"] == 3
    assert (snapshot["succeeded"], snapshot["failed"], snapshot["not_attempted"]) == (1, 2, 0)
    assert isinstance(snapshot["duration_seconds"], int)


def test_collect_snapshot_preserves_zero_stars():
    session = FakeSession(FakeResponse(payload={"stargazers_count": 0}))
    snapshot, _ = ts.collect_snapshot(
        {"a/zero": "2026-10-04"}, "token", date(2026, 10, 4), session=session
    )
    assert snapshot["repos"][0]["status"] == "ok"
    assert snapshot["repos"][0]["stars"] == 0


def test_collect_snapshot_stops_on_rate_limit_and_marks_remaining():
    session = FakeSession(
        FakeResponse(
            403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0"}
        )
    )
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01", "b/two": "2026-10-02"},
        "token",
        date(2026, 10, 4),
        session=session,
    )
    assert [r["status"] for r in snapshot["repos"]] == ["rate_limited", "not_attempted"]
    assert snapshot["rate_limit_remaining"] == 0
    assert snapshot["stop_reason"]
    assert (snapshot["attempted"], snapshot["failed"], snapshot["not_attempted"]) == (1, 1, 1)


def test_collect_snapshot_stops_after_forbidden_response():
    session = FakeSession(FakeResponse(403), FakeResponse(payload={"stargazers_count": 10}))
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01", "b/two": "2026-10-02"},
        "token",
        date(2026, 10, 4),
        session=session,
    )
    assert session.calls == 1
    assert [repo["status"] for repo in snapshot["repos"]] == ["forbidden", "not_attempted"]


def test_rate_limit_remaining_is_not_reused_after_network_failure():
    session = FakeSession(
        FakeResponse(payload={"stargazers_count": 1}, headers={"X-RateLimit-Remaining": "40"}),
        requests.ConnectionError("connection dropped"),
        requests.ConnectionError("connection dropped"),
        requests.ConnectionError("connection dropped"),
    )
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01", "b/two": "2026-10-02"},
        "token",
        date(2026, 10, 4),
        session=session,
    )
    assert snapshot["rate_limit_remaining"] is None


def test_429_response_is_classified_as_rate_limit():
    status, stars, error = ts._status_for_response(FakeResponse(429))
    assert (status, stars) == ("rate_limited", None)
    assert "限流" in error


def test_collect_snapshot_stops_at_request_or_runtime_budget():
    session = FakeSession(
        FakeResponse(payload={"stargazers_count": 1}),
        FakeResponse(payload={"stargazers_count": 2}),
    )
    limited, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01", "b/two": "2026-10-02"},
        "token",
        date(2026, 10, 4),
        max_requests=1,
        session=session,
    )
    assert session.calls == 1
    assert limited["not_attempted"] == 1

    runtime_limited, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01"},
        "token",
        date(2026, 10, 4),
        max_runtime_seconds=0,
    )
    assert runtime_limited["repos"][0]["status"] == "not_attempted"


def test_request_budget_prioritizes_most_recently_seen_repos():
    session = FakeSession(FakeResponse(payload={"stargazers_count": 1}))
    snapshot, _ = ts.collect_snapshot(
        {"a/old": "2026-10-01", "z/new": "2026-10-04"},
        "token",
        date(2026, 10, 4),
        max_requests=1,
        session=session,
    )
    assert snapshot["repos"][0]["name"] == "z/new"
    assert snapshot["repos"][1]["status"] == "not_attempted"


def test_collect_snapshot_records_network_and_invalid_responses():
    session = FakeSession(
        requests.ConnectionError("connection dropped"),
        requests.ConnectionError("connection dropped"),
        requests.ConnectionError("connection dropped"),
        FakeResponse(payload={"stargazers_count": True}),
    )
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01", "b/two": "2026-10-02"},
        "token",
        date(2026, 10, 4),
        session=session,
    )
    assert [r["status"] for r in snapshot["repos"]] == ["network_error", "invalid_response"]
    assert [r["stars"] for r in snapshot["repos"]] == [None, None]


def test_get_with_retry_retries_server_errors_then_records_failure(monkeypatch):
    monkeypatch.setattr(ts, "RETRY_ATTEMPTS", 2)
    session = FakeSession(FakeResponse(503), FakeResponse(503))
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01"}, "token", date(2026, 10, 4), session=session
    )
    assert session.calls == 2
    assert snapshot["repos"][0]["status"] == "network_error"


def test_http_request_budget_includes_retries(monkeypatch):
    monkeypatch.setattr(ts, "RETRY_ATTEMPTS", 3)
    session = FakeSession(FakeResponse(503), FakeResponse(503), FakeResponse(200, {"stargazers_count": 5}))
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-01", "b/two": "2026-10-02"},
        "token",
        date(2026, 10, 4),
        max_requests=2,
        session=session,
    )
    assert session.calls == 2
    assert snapshot["http_requests"] == 2
    assert [repo["status"] for repo in snapshot["repos"]] == ["network_error", "not_attempted"]


def test_write_snapshot_validates_and_replaces_atomically(tmp_path):
    snapshot = {
        "schema_version": 1,
        "date": "2026-10-04",
        "fetched_at": "2026-10-04T00:00:00+00:00",
        "source": "github-repos-api",
        "candidate_policy": "official-trending-last-seen-30d",
        "candidate_count": 1,
        "attempted": 1,
        "http_requests": 1,
        "succeeded": 1,
        "failed": 0,
        "not_attempted": 0,
        "duration_seconds": 2,
        "repos": [{
            "name": "a/one", "stars": 1, "status": "ok", "last_seen": "2026-10-04"
        }],
    }
    path = ts.write_snapshot(snapshot, tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-04.json"]


def test_write_snapshot_rejects_invalid_data_without_replacing_previous(tmp_path):
    valid = {
        "schema_version": 1,
        "date": "2026-10-04",
        "fetched_at": "2026-10-04T00:00:00+00:00",
        "source": "github-repos-api",
        "candidate_policy": "official-trending-last-seen-30d",
        "candidate_count": 0,
        "attempted": 0,
        "http_requests": 0,
        "succeeded": 0,
        "failed": 0,
        "not_attempted": 0,
        "duration_seconds": 0,
        "repos": [],
    }
    path = ts.write_snapshot(valid, tmp_path)
    invalid = {**valid, "candidate_count": 1}
    try:
        ts.write_snapshot(invalid, tmp_path)
    except ValueError as exc:
        assert "候选数与仓库记录数不一致" in str(exc)
    else:
        raise AssertionError("Expected invalid snapshot to be rejected")
    assert json.loads(path.read_text(encoding="utf-8")) == valid
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10-04.json"]


def test_request_uses_bearer_token_without_logging_it():
    session = FakeSession(FakeResponse(payload={"stargazers_count": 1}))
    snapshot, _ = ts.collect_snapshot(
        {"a/one": "2026-10-04"}, "secret-token", date(2026, 10, 4), session=session
    )
    assert session.request_headers[0]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in json.dumps(snapshot)


def test_main_requires_token_and_does_not_write_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = ts.main(["--data-dir", str(tmp_path), "--out-dir", str(tmp_path / "out")])
    assert result == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_main_writes_empty_candidate_snapshot_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    result = ts.main([
        "--data-dir", str(tmp_path),
        "--out-dir", str(tmp_path / "out"),
        "--date", "2026-10-04",
    ])
    snapshot = json.loads((tmp_path / "out" / "2026-10-04.json").read_text(encoding="utf-8"))
    assert result == 0
    assert snapshot["candidate_count"] == 0
    assert snapshot["repos"] == []
