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

    assert "无变化" in capsys.readouterr().out
    s = json.loads(out.read_text(encoding="utf-8"))
    assert (s["first"], s["last"], s["count"]) == ("2026-10-01", "2026-10-02", 2)
    assert s["days"]["2026-10-02"]["top"] == 90
    assert s["peak"] == {"date": "2026-10-02", "top": 90}
    assert s["appear"][0] == {"n": "a/b", "days": 2, "best": 90, "best_date": "2026-10-02"}
    assert s["langs"]["rows"][0] == ["Rust", 3] and s["langs"]["total"] == 1


def test_main_respects_top_items_truncation(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    items = [
        {"name": f"o/p{i}", "stars": "1", "period_stars": f"+{100 - i}", "lang": "", "desc": ""}
        for i in range(20)
    ]
    _write_snapshot(data / "2026-10-01.json", "2026-10-01", items)
    out = tmp_path / "summary.json"
    monkeypatch.setattr(
        "sys.argv",
        ["build_dashboard.py", "--data-dir", str(data), "--out", str(out), "--top-items", "5"],
    )
    assert bd.main() == 0
    s = json.loads(out.read_text(encoding="utf-8"))
    assert len(s["days"]["2026-10-01"]["items"]) == 5
    # 截断不改变当天口径统计：top/sum 仍按完整榜单计算
    assert s["days"]["2026-10-01"]["top"] == 100
    assert s["days"]["2026-10-01"]["n"] == 20
