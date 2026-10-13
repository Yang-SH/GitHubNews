"""build_dashboard 的回归测试：字段解析、快照规范化、聚合与幂等输出。"""
import json

import build_dashboard as bd


# ---------------------------------------------------------------------------
# 数字与摘要解析
# ---------------------------------------------------------------------------

def test_to_int_handles_formatted_numbers():
    assert bd.to_int("1,568") == 1568
    assert bd.to_int("+734") == 734
    assert bd.to_int(42) == 42
    assert bd.to_int("") is None
    assert bd.to_int(None) is None
    assert bd.to_int("abc") is None


def test_compact_desc_collapses_and_truncates():
    assert bd.compact_desc("  a\n b  c ", 90) == "a b c"
    long = "x" * 100
    out = bd.compact_desc(long, 90)
    assert len(out) == 90 and out.endswith("…")


# ---------------------------------------------------------------------------
# 快照规范化
# ---------------------------------------------------------------------------

def test_load_day_normalizes_official_and_backfill(tmp_path):
    official = tmp_path / "2026-10-11.json"
    official.write_text(json.dumps({
        "date": "2026-10-11", "fetched_at": "t",
        "daily": [
            {"name": "a/b", "url": "u", "desc": "d", "lang": "Rust",
             "stars": "1,000", "period_stars": "+100"},
            {"name": "c/d", "url": "u", "desc": "", "lang": "",
             "stars": "500", "period_stars": "+50"},
        ],
    }), encoding="utf-8")
    backfill = tmp_path / "2025-01-01.json"
    backfill.write_text(json.dumps({
        "date": "2025-01-01", "fetched_at": "t",
        "source": "gharchive-watchevent", "quality": "degraded",
        "daily": [{"rank": 1, "name": "e/f", "url": "u", "desc": "", "lang": "",
                   "stars": "10", "period_stars": "+9"}],
    }), encoding="utf-8")

    day = bd.load_day(str(official))
    assert day["src"] == "official" and day["q"] is None
    assert day["n"] == 2 and day["top"] == 100 and day["sum"] == 150
    assert day["lang_counts"] == {"Rust": 1}
    assert day["items"][0] == {"n": "a/b", "s": 1000, "p": 100, "l": "Rust", "d": "d"}

    day = bd.load_day(str(backfill))
    assert day["src"] == "gharchive-watchevent" and day["q"] == "degraded"
    assert day["top"] == 9


def test_load_day_skips_broken_files(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"date": "2026-01-01", "daily": []}), encoding="utf-8")
    assert bd.load_day(str(bad)) is None
    assert bd.load_day(str(empty)) is None


# ---------------------------------------------------------------------------
# 聚合与幂等
# ---------------------------------------------------------------------------

def _write_snapshot(path, date, items, **extra):
    path.write_text(
        json.dumps({"date": date, "fetched_at": "t", **extra, "daily": items}),
        encoding="utf-8",
    )


def test_main_builds_aggregate_and_is_idempotent(tmp_path, capsys, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _write_snapshot(data / "2026-10-01.json", "2026-10-01", [
        {"name": "a/b", "stars": "100", "period_stars": "+30", "lang": "Rust", "desc": "x"},
        {"name": "c/d", "stars": "50", "period_stars": "+20", "lang": "Rust", "desc": "y"},
    ], source="gharchive-watchevent", quality="good")
    _write_snapshot(data / "2026-10-02.json", "2026-10-02", [
        {"name": "a/b", "stars": "200", "period_stars": "+90", "lang": "Rust", "desc": "x"},
    ])
    out = tmp_path / "dashboard" / "summary.json"

    for _ in range(2):  # 第二次应报告无变化
        monkeypatch.setattr(
            "sys.argv",
            ["build_dashboard.py", "--data-dir", str(data), "--out", str(out)],
        )
        assert bd.main() == 0

    output = capsys.readouterr().out
    assert "无变化" in output
    assert "2 份快照" in output
    s = json.loads(out.read_text(encoding="utf-8"))
    official = s["cohorts"]["official"]
    backfill = s["cohorts"]["gharchive-good"]
    assert (official["first"], official["last"], official["count"]) == (
        "2026-10-02", "2026-10-02", 1
    )
    assert official["days"]["2026-10-02"]["top"] == 90
    assert official["peak"] == {"date": "2026-10-02", "top": 90}
    assert official["appear"][0] == {
        "n": "a/b", "days": 1, "best": 90, "best_date": "2026-10-02"
    }
    assert official["langs"]["rows"][0] == ["Rust", 1]
    assert [row["date"] for row in s["official_trends"]["daily"]] == ["2026-10-02"]
    assert [repo["name"] for repo in s["star_tracking"]["repos"]] == ["a/b"]
    assert backfill["count"] == 1
    assert backfill["appear"][0]["days"] == 1
    assert backfill["langs"]["rows"][0] == ["Rust", 2]


def test_main_keeps_all_official_items_and_truncates_backfill_display(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    items = [
        {"name": f"o/p{i}", "stars": "1", "period_stars": f"+{100 - i}", "lang": "Rust", "desc": ""}
        for i in range(20)
    ]
    _write_snapshot(data / "2026-10-01.json", "2026-10-01", items)
    _write_snapshot(
        data / "2025-01-01.json", "2025-01-01", items,
        source="gharchive-watchevent", quality="good",
    )
    _write_snapshot(
        data / "2025-01-02.json", "2025-01-02", items,
        source="gharchive-watchevent", quality="degraded",
    )
    out = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        ["build_dashboard.py", "--data-dir", str(data), "--out", str(out), "--top-items", "5"],
    )
    assert bd.main() == 0
    s = json.loads(out.read_text(encoding="utf-8"))
    official = s["cohorts"]["official"]["days"]["2026-10-01"]
    backfill = s["cohorts"]["gharchive-good"]["days"]["2025-01-01"]
    degraded = s["cohorts"]["gharchive-degraded"]["days"]["2025-01-02"]
    assert len(official["items"]) == 20 and official["shown"] == 20
    assert len(backfill["items"]) == 5 and backfill["shown"] == 5
    assert len(degraded["items"]) == 5 and degraded["shown"] == 5
    # 展示截断不改变完整榜单统计与语言分布。
    assert backfill["top"] == 100
    assert backfill["n"] == 20
    assert backfill["lang_counts"] == {"Rust": 20}
    assert s["cohorts"]["gharchive-good"]["langs"]["rows"][0] == ["Rust", 20]
    assert s["cohorts"]["gharchive-degraded"]["count"] == 1


def test_cohort_key_separates_backfill_quality():
    assert bd.cohort_key("official", None) == "official"
    assert bd.cohort_key("gharchive-watchevent", "good") == "gharchive-good"
    assert bd.cohort_key("gharchive-watchevent", "degraded") == "gharchive-degraded"


def test_build_official_trends_counts_new_repeated_and_rank_changes():
    trends = bd.build_official_trends({
        "2026-10-01": {
            "n": 2,
            "items": [{"n": "a/one"}, {"n": "b/two"}],
        },
        "2026-10-03": {
            "n": 2,
            "items": [{"n": "b/two"}, {"n": "c/three"}],
        },
    })
    comparison = trends["daily"][1]["comparison"]
    assert comparison == {
        "previous_date": "2026-10-01",
        "gap_days": 2,
        "new_repos": ["c/three"],
        "repeated_count": 1,
        "rank_changes": [{"n": "b/two", "from": 2, "to": 1, "change": 1}],
    }
    assert trends["windows"]["7d"]["snapshots"] == 2
    assert trends["windows"]["7d"]["comparable_snapshots"] == 1
    assert trends["windows"]["7d"]["new_entry_count"] == 1
    assert trends["windows"]["30d"]["coverage"] == 2 / 30


def test_official_candidates_use_only_the_last_30_calendar_days():
    assert bd.official_candidates({
        "2026-09-01": {"items": [{"n": "old/repo"}]},
        "2026-09-02": {"items": [{"n": "a/one"}]},
        "2026-10-01": {"items": [{"n": "b/two"}]},
    }) == {"a/one", "b/two"}


def test_star_change_window_boundaries_and_negative_delta():
    observations = [
        {"date": "2026-09-23", "status": "ok", "stars": 115},
        {"date": "2026-10-01", "status": "ok", "stars": 90},
    ]
    assert bd._star_change(observations, 7) == {
        "status": "ok",
        "baseline_date": "2026-09-23",
        "end_date": "2026-10-01",
        "interval_days": 8,
        "delta": -25,
        "observed_days": 2,
        "expected_days": 9,
    }
    lower_edge = [
        {"date": "2026-09-25", "status": "ok", "stars": 100},
        {"date": "2026-10-01", "status": "ok", "stars": 105},
    ]
    assert bd._star_change(lower_edge, 7)["interval_days"] == 6
    outside_edge = [
        {"date": "2026-09-22", "status": "ok", "stars": 100},
        {"date": "2026-10-01", "status": "ok", "stars": 105},
    ]
    assert bd._star_change(outside_edge, 7) == {
        "status": "insufficient", "end_date": "2026-10-01", "valid_observations": 2
    }
    thirty_day_observations = [
        {"date": "2026-09-04", "status": "ok", "stars": 100},
        {"date": "2026-10-01", "status": "ok", "stars": 90},
    ]
    assert bd._star_change(thirty_day_observations, 30) == {
        "status": "ok",
        "baseline_date": "2026-09-04",
        "end_date": "2026-10-01",
        "interval_days": 27,
        "delta": -10,
        "observed_days": 2,
        "expected_days": 28,
    }
    upper_edge = [
        {"date": "2026-08-29", "status": "ok", "stars": 100},
        {"date": "2026-10-01", "status": "ok", "stars": 105},
    ]
    assert bd._star_change(upper_edge, 30)["interval_days"] == 33


def test_star_change_ignores_failed_observations_and_checks_30d_edges():
    observations = [
        {"date": "2026-09-01", "status": "ok", "stars": 100},
        {"date": "2026-09-04", "status": "not_attempted", "stars": None},
        {"date": "2026-09-28", "status": "ok", "stars": 90},
        {"date": "2026-10-01", "status": "rate_limited", "stars": None},
    ]
    assert bd._star_change(observations, 30) == {
        "status": "ok",
        "baseline_date": "2026-09-01",
        "end_date": "2026-09-28",
        "interval_days": 27,
        "delta": -10,
        "observed_days": 2,
        "expected_days": 28,
    }


def test_load_star_tracking_filters_to_active_repos_and_skips_invalid(tmp_path, capsys):
    stars_dir = tmp_path / "stars"
    stars_dir.mkdir()
    def star_snapshot(day, repos):
        succeeded = sum(repo["status"] == "ok" for repo in repos)
        failed = sum(repo["status"] not in ("ok", "not_attempted") for repo in repos)
        not_attempted = sum(repo["status"] == "not_attempted" for repo in repos)
        return {
            "schema_version": 1,
            "date": day,
            "fetched_at": f"{day}T00:00:00+00:00",
            "source": "github-repos-api",
            "candidate_policy": "official-trending-last-seen-30d",
            "candidate_count": len(repos),
            "attempted": succeeded + failed,
            "http_requests": succeeded + failed,
            "succeeded": succeeded,
            "failed": failed,
            "not_attempted": not_attempted,
            "repos": repos,
        }

    (stars_dir / "2026-10-01.json").write_text(json.dumps(star_snapshot(
        "2026-10-01", [
            {"name": "a/one", "stars": 100, "status": "ok", "last_seen": "2026-10-01"},
            {"name": "b/two", "stars": 200, "status": "ok", "last_seen": "2026-10-01"},
        ],
    )), encoding="utf-8")
    (stars_dir / "2026-10-02.json").write_text(json.dumps(star_snapshot(
        "2026-10-02", [
            {"name": "a/one", "stars": 125, "status": "ok", "last_seen": "2026-10-02"},
            {"name": "b/two", "stars": None, "status": "rate_limited", "last_seen": "2026-10-02"},
        ],
    )), encoding="utf-8")
    (stars_dir / "2026-10-03.json").write_text("{broken", encoding="utf-8")
    (stars_dir / "2026-10-04.json").write_text(json.dumps({
        "schema_version": 1,
        "date": "2026-10-04",
        "source": "github-repos-api",
        "candidate_policy": "official-trending-last-seen-30d",
        "candidate_count": 1,
        "repos": [],
    }), encoding="utf-8")

    summary = bd.load_star_tracking(str(stars_dir), {"a/one", "c/three"})
    assert summary["snapshot_count"] == 2
    assert [repo["name"] for repo in summary["repos"]] == ["a/one", "c/three"]
    assert summary["repos"][0]["change_7d"] == {
        "status": "insufficient", "end_date": "2026-10-02", "valid_observations": 2
    }
    assert summary["repos"][1]["observations"] == []
    assert "跳过无效 Star 快照" in capsys.readouterr().err


def test_main_aggregates_star_files_separately_from_backfill(tmp_path, monkeypatch):
    data = tmp_path / "data"
    stars = data / "repo-stars"
    stars.mkdir(parents=True)
    _write_snapshot(data / "2026-09-30.json", "2026-09-30", [
        {"name": "history/repo", "stars": "500", "period_stars": "+5"},
    ], source="gharchive-watchevent", quality="good")
    _write_snapshot(data / "2026-10-01.json", "2026-10-01", [
        {"name": "official/repo", "stars": "100", "period_stars": "+10"},
    ])
    star_snapshot = {
        "schema_version": 1,
        "date": "2026-10-01",
        "fetched_at": "2026-10-01T00:00:00+00:00",
        "source": "github-repos-api",
        "candidate_policy": "official-trending-last-seen-30d",
        "candidate_count": 1,
        "attempted": 1,
        "http_requests": 1,
        "succeeded": 1,
        "failed": 0,
        "not_attempted": 0,
        "repos": [{
            "name": "official/repo",
            "stars": 125,
            "status": "ok",
            "last_seen": "2026-10-01",
        }],
    }
    (stars / "2026-10-01.json").write_text(json.dumps(star_snapshot), encoding="utf-8")
    out = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        ["build_dashboard.py", "--data-dir", str(data), "--out", str(out)],
    )

    assert bd.main() == 0

    summary = json.loads(out.read_text(encoding="utf-8"))
    assert [row["date"] for row in summary["official_trends"]["daily"]] == ["2026-10-01"]
    assert [row["name"] for row in summary["star_tracking"]["repos"]] == ["official/repo"]
    assert summary["star_tracking"]["repos"][0]["observations"] == [{
        "date": "2026-10-01", "stars": 125, "status": "ok"
    }]


def test_main_fails_when_all_snapshots_are_invalid(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "2026-10-01.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["build_dashboard.py", "--data-dir", str(data), "--out", str(tmp_path / "summary.json")],
    )
    try:
        bd.main()
    except SystemExit as exc:
        assert "没有有效快照" in str(exc)
    else:
        raise AssertionError("Expected all-invalid snapshots to fail")
