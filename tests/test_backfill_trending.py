"""backfill_trending 的回归测试：分月切块、TSV 解析、总 Star 累计与快照组装。"""
import json
from datetime import date

import pytest

import backfill_trending as bt


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(bt, "RETRY_BACKOFF_SECONDS", 0.01)


class FakeStreamResp:
    """模拟 post_query 返回的流式响应（支持 with 与逐行迭代）。"""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self, decode_unicode=False):
        yield from self._lines


# ---------------------------------------------------------------------------
# 日期切块
# ---------------------------------------------------------------------------

def test_month_chunks_spans_partial_months_and_years():
    chunks = bt.month_chunks(date(2025, 11, 15), date(2026, 2, 3))
    assert chunks == [
        (date(2025, 11, 15), date(2025, 11, 30)),
        (date(2025, 12, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 3)),
    ]


def test_month_chunks_single_month_and_empty_range():
    assert bt.month_chunks(date(2025, 6, 1), date(2025, 6, 30)) == [
        (date(2025, 6, 1), date(2025, 6, 30))
    ]
    assert bt.month_chunks(date(2025, 6, 10), date(2025, 6, 5)) == []


def test_default_end_is_yesterday():
    assert bt.default_end(date(2026, 10, 7)) == date(2026, 10, 6)


# ---------------------------------------------------------------------------
# 榜单抓取与解析
# ---------------------------------------------------------------------------

def test_fetch_month_top_parses_tsv_and_skips_blank_lines(monkeypatch):
    captured = {}

    def fake_post_query(session, sql):
        captured["sql"] = sql
        return FakeStreamResp(
            ["2025-06-01\ta/b\t12", "", "2025-06-01\tc/d\t5", "2025-06-02\ta/b\t7"]
        )

    monkeypatch.setattr(bt, "post_query", fake_post_query)
    top = bt.fetch_month_top(None, date(2025, 6, 1), date(2025, 6, 2), 100)

    assert top == {
        "2025-06-01": [("a/b", 12), ("c/d", 5)],
        "2025-06-02": [("a/b", 7)],
    }
    # SQL 骨架：WatchEvent 过滤、日期边界、确定性平局排序与 LIMIT n BY day
    assert "event_type = 'WatchEvent'" in captured["sql"]
    assert "created_at >= '2025-06-01 00:00:00'" in captured["sql"]
    assert "created_at <  '2025-06-03 00:00:00'" in captured["sql"]
    assert "ORDER BY day ASC, stars DESC, repo_name ASC" in captured["sql"]
    assert "LIMIT 100 BY day" in captured["sql"]


# ---------------------------------------------------------------------------
# 总 Star 累计与条目组装
# ---------------------------------------------------------------------------

def test_repo_in_chunks_batches_and_escapes():
    repos = [f"o/r{i}" for i in range(bt.IN_CHUNK + 1)]
    chunks = bt._repo_in_chunks(repos)
    assert len(chunks) == 2
    assert chunks[0].count(",") == bt.IN_CHUNK - 1
    # 单引号与反斜杠需转义
    assert bt._repo_in_chunks(["a'b/c\\d"]) == ["'a\\'b/c\\\\d'"]


def test_quality_zones():
    assert bt.quality_for(date(2025, 1, 1)) == "good"
    assert bt.quality_for(date(2025, 10, 31)) == "good"
    assert bt.quality_for(date(2025, 11, 1)) == "degraded"
    assert bt.quality_for(date(2026, 3, 31)) == "degraded"


def test_resolve_totals_accumulates_baseline_and_daily_counts():
    baseline = {"a": 10}
    grid = {
        "2025-06-01": {"a": 5, "b": 100},
        "2025-06-02": {"a": 7},  # b 当日 0 新增，无网格行
    }
    top_by_day = {
        "2025-06-01": [("a", 5)],
        "2025-06-02": [("a", 7), ("b", 1)],
    }
    totals = bt.resolve_totals(baseline, grid, top_by_day)

    assert totals[("a", "2025-06-01")] == 15  # 10 基线 + 当日 5
    assert totals[("a", "2025-06-02")] == 22
    assert totals[("b", "2025-06-02")] == 100  # 当日 0 新增，总 Star 保持


def test_build_items_formats_and_ranks(monkeypatch):
    rows = [("a/b", 1234), ("c/d", 57)]
    totals = {("a/b", "2025-06-01"): 9123456}
    items = bt.build_items(rows, "2025-06-01", totals)

    assert [it["rank"] for it in items] == [1, 2]
    assert items[0]["name"] == "a/b"
    assert items[0]["url"] == "https://github.com/a/b"
    assert items[0]["stars"] == "9,123,456"
    assert items[0]["period_stars"] == "+1,234"
    # 总 Star 未知的条目降级为 "-"；回填条目无 desc/lang/readme
    assert items[1]["stars"] == "-"
    assert items[0]["desc"] == "" and items[0]["lang"] == ""
    assert "readme" not in items[0]


def test_snapshot_doc_and_write_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "DATA_DIR", tmp_path)
    fetched = bt.now_bj().replace(microsecond=0)
    doc = bt.snapshot_doc("2025-06-01", [{"rank": 1, "name": "a/b"}], fetched)
    out = bt.write_snapshot("2025-06-01", doc)

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["date"] == "2025-06-01"
    assert saved["fetched_at"] == fetched.isoformat()
    assert saved["source"] == "gharchive-watchevent"
    assert saved["quality"] == "good"
    assert saved["daily"] == [{"rank": 1, "name": "a/b"}]
