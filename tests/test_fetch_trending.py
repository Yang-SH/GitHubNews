"""fetch_trending 的回归测试：重试路径、解析、渲染与 README 索引区块。"""
import json
from datetime import datetime

import pytest
import requests

import fetch_trending as ft

BJ = ft.BEIJING


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(ft, "RETRY_BACKOFF_SECONDS", 0.01)


class FakeResp:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code, self.text, self._json = status_code, text, json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error")


class FakeSession:
    """按顺序回放响应；响应位置也可以放异常对象。耗尽后若给出 default 则重复返回。"""

    def __init__(self, *responses, default=None):
        self.responses = list(responses)
        self.default = default
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        if self.responses:
            r = self.responses.pop(0)
        elif self.default is not None:
            r = self.default
        else:
            raise AssertionError("no more fake responses")
        if isinstance(r, Exception):
            raise r
        return r


VALID_TRENDING = """
<article class="Box-row">
  <h2><a href="/foo/bar"> foo/bar </a></h2>
  <p>desc with a | pipe</p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/foo/bar/stargazers">1,234</a>
  <span>1,234 stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/baz/qux"> baz/qux </a></h2>
  <p>another desc</p>
  <span itemprop="programmingLanguage">Rust</span>
  <a href="/baz/qux/stargazers">9,000</a>
  <span>800 stars today</span>
</article>
"""

VALID_API = {"items": [{
    "full_name": "a/b", "html_url": "https://github.com/a/b",
    "description": "d", "language": "Python",
    "stargazers_count": 1234, "created_at": "2026-01-01T00:00:00Z",
}]}


# ---------------------------------------------------------------------------
# 重试助手
# ---------------------------------------------------------------------------

def test_get_with_retry_5xx_then_success():
    s = FakeSession(FakeResp(503), FakeResp(503), FakeResp(text="ok"))
    assert ft.get_with_retry(s, "https://x").text == "ok"
    assert s.calls == 3


def test_get_with_retry_exhausts_5xx():
    s = FakeSession(FakeResp(503), FakeResp(502), FakeResp(500))
    with pytest.raises(requests.HTTPError):
        ft.get_with_retry(s, "https://x")
    assert s.calls == 3


def test_get_with_retry_4xx_not_retried():
    s = FakeSession(FakeResp(404))
    resp = ft.get_with_retry(s, "https://x")
    assert resp.status_code == 404 and s.calls == 1


def test_get_with_retry_network_error_then_success():
    s = FakeSession(requests.ConnectionError("eof"), FakeResp(text="ok"))
    assert ft.get_with_retry(s, "https://x").text == "ok"
    assert s.calls == 2


# ---------------------------------------------------------------------------
# Trending 抓取与解析
# ---------------------------------------------------------------------------

def test_scrape_trending_parses_fields():
    s = FakeSession(FakeResp(text=VALID_TRENDING))
    items = ft.scrape_trending("daily", s)
    assert items[0] == {
        "name": "foo/bar", "url": "https://github.com/foo/bar",
        "desc": "desc with a | pipe", "lang": "Python",
        "stars": "1,234", "period_stars": "+1,234",
    }
    assert items[1]["name"] == "baz/qux" and items[1]["lang"] == "Rust"


def test_scrape_trending_empty_page_retries_then_raises():
    s = FakeSession(*[FakeResp(text="<html></html>")] * ft.RETRY_ATTEMPTS)
    with pytest.raises(RuntimeError, match="解析结果为空"):
        ft.scrape_trending("daily", s)
    assert s.calls == ft.RETRY_ATTEMPTS


def test_scrape_trending_503_recovers_via_retry():
    s = FakeSession(FakeResp(503), FakeResp(503), FakeResp(text=VALID_TRENDING))
    assert len(ft.scrape_trending("daily", s)) == 2
    assert s.calls == 3


# ---------------------------------------------------------------------------
# 年报（Search API）
# ---------------------------------------------------------------------------

def test_fetch_yearly_retry_and_fields(monkeypatch):
    monkeypatch.setattr(ft, "YEARLY_ATTEMPTS", 1)
    monkeypatch.setattr(ft, "YEARLY_PAGES", 1)
    s = FakeSession(FakeResp(503), default=FakeResp(json_data=VALID_API))
    items = ft.fetch_yearly(s, None)
    assert s.calls == 2
    assert items == [{
        "name": "a/b", "url": "https://github.com/a/b", "desc": "d",
        "lang": "Python", "stars": "1,234", "created": "2026-01-01",
    }]


def test_fetch_yearly_uses_token_header():
    captured = {}

    class S(FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            captured["headers"] = headers
            return FakeResp(json_data=VALID_API)

    ft.fetch_yearly(S(), "tok-123")
    assert captured["headers"]["Authorization"] == "Bearer tok-123"


def _fake_api_page(n, star_base=1000):
    return {"items": [
        {"full_name": f"r{i}", "html_url": f"https://github.com/r{i}", "description": f"d{i}",
         "language": "Python", "stargazers_count": star_base - i,
         "created_at": "2026-01-01T00:00:00Z"}
        for i in range(n)
    ]}


def test_fetch_yearly_merges_pages_dedupes_and_sorts_locally(monkeypatch):
    monkeypatch.setattr(ft, "YEARLY_ATTEMPTS", 1)
    page1 = _fake_api_page(100)  # r0..r99，Star 1000..901
    page2 = {"items": [
        {**page1["items"][0], "stargazers_count": 2000, "full_name": "r200",
         "html_url": "https://github.com/r200"},  # 第二页出现更高 Star 的新仓库
        {**page1["items"][5], "stargazers_count": 5},  # 重复仓库（更低数值），应去重保留先见值
    ]}
    s = FakeSession(FakeResp(json_data=page1), FakeResp(json_data=page2))
    items = ft.fetch_yearly(s, None)
    assert s.calls == 2
    assert len(items) == 25
    assert items[0]["name"] == "r200" and items[0]["stars"] == "2,000"
    assert items[1]["name"] == "r0"
    assert sum(1 for it in items if it["name"] == "r5") == 1
    assert all("_stars" not in it for it in items)


def test_fetch_yearly_unions_across_attempts(monkeypatch):
    # 不同轮次命中不同索引副本：合并后应取到两轮里 Star 最高的仓库
    monkeypatch.setattr(ft, "YEARLY_ATTEMPTS", 2)
    monkeypatch.setattr(ft, "YEARLY_PAGES", 1)
    s = FakeSession(
        FakeResp(json_data={"items": [
            {"full_name": "a/one", "html_url": "u", "description": "", "language": "Go",
             "stargazers_count": 100, "created_at": "2026-01-01T00:00:00Z"},
            {"full_name": "b/two", "html_url": "u", "description": "", "language": "Go",
             "stargazers_count": 50, "created_at": "2026-01-01T00:00:00Z"},
        ]}),
        FakeResp(json_data={"items": [
            {"full_name": "c/three", "html_url": "u", "description": "", "language": "Go",
             "stargazers_count": 200, "created_at": "2026-01-01T00:00:00Z"},
        ]}),
    )
    items = ft.fetch_yearly(s, None)
    assert s.calls == 2
    assert [it["name"] for it in items] == ["c/three", "a/one", "b/two"]
    assert items[0]["stars"] == "200"


def test_fetch_yearly_second_page_failure_degrades(monkeypatch):
    monkeypatch.setattr(ft, "YEARLY_ATTEMPTS", 1)
    s = FakeSession(FakeResp(json_data=_fake_api_page(100)), FakeResp(404))
    items = ft.fetch_yearly(s, None)
    assert s.calls == 2  # 第二页失败不拖垮整份年报
    assert len(items) == 25 and items[0]["stars"] == "1,000"


def test_fetch_yearly_first_page_failure_raises():
    s = FakeSession(FakeResp(503), FakeResp(502), FakeResp(500))
    with pytest.raises(requests.HTTPError):
        ft.fetch_yearly(s, None)
    assert s.calls == 3


# ---------------------------------------------------------------------------
# README 摘录
# ---------------------------------------------------------------------------

def test_readme_excerpt_success_truncated():
    s = FakeSession(FakeResp(text="R" * 3000))
    assert ft.fetch_readme_excerpt(s, "a/b", None) == "R" * ft.SNAPSHOT_README_CHARS


def test_readme_excerpt_500_retries_then_success():
    s = FakeSession(FakeResp(500), FakeResp(text="hello"))
    assert ft.fetch_readme_excerpt(s, "a/b", None) == "hello"
    assert s.calls == 2


def test_readme_excerpt_403_no_retry_returns_empty():
    s = FakeSession(FakeResp(403))
    assert ft.fetch_readme_excerpt(s, "a/b", None) == ""
    assert s.calls == 1  # 4xx 不重试


def test_readme_excerpt_network_error_returns_empty():
    s = FakeSession(requests.ConnectionError("x"), requests.ConnectionError("x"))
    assert ft.fetch_readme_excerpt(s, "a/b", None) == ""
    assert s.calls == 2  # 摘录重试上限为 2


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _sample_items():
    return [
        {"name": "a/one", "url": "u1", "desc": "d1", "lang": "Python", "stars": "100", "period_stars": "+10"},
        {"name": "b/two", "url": "u2", "desc": "d2", "lang": "Rust", "stars": "90", "period_stars": "+9"},
        {"name": "c/three", "url": "u3", "desc": "d3", "lang": "Python", "stars": "80", "period_stars": "+8"},
    ]


def test_group_by_lang_keeps_first_appearance_order():
    groups = ft.group_by_lang(_sample_items())
    assert [lang for lang, _ in groups] == ["Python", "Rust"]
    assert groups[0][1][0]["name"] == "a/one"


def test_render_table_escapes_pipes_and_truncates():
    # 管道符在前 80 字符内（会被转义），总长超 80（会被截断）
    rows = [[1, "a/b", "u", "x" * 30 + " | tail" + "y" * 100, "Lang", "1", "+1"]]
    lines = ft.render_table(rows)
    assert "\\|" in lines[2] and "..." in lines[2] and len(lines) == 3


def test_render_report_daily_title_and_global_rank():
    meta = ft.TRENDING_META["daily"]
    text = ft.render_report("daily", _sample_items(), meta, datetime(2026, 10, 6, 8, 0, tzinfo=BJ))
    assert "# GitHub 每日热点 — 2026-10-06" in text
    assert "表中 # 为总榜排名" in text
    # Rust 小节里的项目总榜排名应为 2，而非组内重新编号的 1
    rust_section = text.split("## Rust")[1]
    assert rust_section.strip().splitlines()[2].startswith("| 2 |")


def test_render_report_monthly_title_uses_year_month():
    meta = ft.TRENDING_META["monthly"]
    text = ft.render_report("monthly", _sample_items(), meta, datetime(2026, 10, 6, 8, 0, tzinfo=BJ))
    assert "# GitHub 每月热点 — 2026-10" in text


def test_render_report_yearly_global_rank():
    items = [{**it, "created": "2026-01-01"} for it in _sample_items()]
    text = ft.render_report_yearly(items, datetime(2026, 10, 6, 8, 0, tzinfo=BJ))
    assert "# GitHub 年度热点 — 2026" in text
    assert "创建日期" in text
    rust_section = text.split("## Rust")[1]
    assert rust_section.strip().splitlines()[2].startswith("| 2 |")


# ---------------------------------------------------------------------------
# 快照与 README 索引
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "ROOT", tmp_path)
    monkeypatch.setattr(ft, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(ft, "DATA_DIR", tmp_path / "data")
    return tmp_path


def test_write_snapshot_logs_readme_coverage(repo, capsys):
    data = {"daily": [
        {"name": "a/b", "readme": "r"},
        {"name": "c/d", "readme": ""},
    ]}
    ft.write_snapshot(data, datetime(2026, 10, 6, 8, 0, tzinfo=BJ))
    out = capsys.readouterr().out
    assert "README 摘录 1/2" in out
    saved = json.loads((repo / "data/2026-10-06.json").read_text(encoding="utf-8"))
    assert saved["daily"][0]["readme"] == "r"


def test_update_readme_first_run_creates_markers(repo):
    (repo / "reports/daily").mkdir(parents=True)
    (repo / "reports/daily/2026-10-06.md").write_text("# d", encoding="utf-8")
    ft.update_readme(datetime(2026, 10, 6, 8, 0, tzinfo=BJ))
    text = (repo / "README.md").read_text(encoding="utf-8")
    assert ft.REPORTS_MARKER[0] in text and ft.ANALYSIS_MARKER[0] in text
    assert "[2026-10-06](reports/daily/2026-10-06.md)" in text
    assert "## 项目分析" in text and "暂无" in text


def test_update_readme_preserves_manual_content(repo):
    (repo / "reports/daily").mkdir(parents=True)
    (repo / "reports/daily/2026-10-06.md").write_text("# d", encoding="utf-8")
    ft.update_readme(datetime(2026, 10, 6, 8, 0, tzinfo=BJ))

    readme = repo / "README.md"
    text = readme.read_text(encoding="utf-8")
    text = text.replace(
        "由 GitHub Actions", "手写前言：由 GitHub Actions"
    ) + "\n## 开发\n\n本地跑 `uv run scripts/fetch_trending.py`。\n"
    readme.write_text(text, encoding="utf-8")

    # 新增一份报告后再运行：索引更新，手写内容保留
    (repo / "reports/daily/2026-10-07.md").write_text("# d2", encoding="utf-8")
    ft.update_readme(datetime(2026, 10, 7, 8, 0, tzinfo=BJ))
    new_text = readme.read_text(encoding="utf-8")
    assert "手写前言" in new_text
    assert "## 开发" in new_text
    assert "[2026-10-07](reports/daily/2026-10-07.md)" in new_text
    assert "[2026-10-06](reports/daily/2026-10-06.md)" in new_text


def test_update_readme_regenerates_when_markers_missing(repo):
    (repo / "README.md").write_text("# 旧格式 README，没有标记区块\n", encoding="utf-8")
    (repo / "reports/yearly").mkdir(parents=True)
    (repo / "reports/yearly/2026.md").write_text("# y", encoding="utf-8")
    ft.update_readme(datetime(2026, 10, 6, 8, 0, tzinfo=BJ))
    text = (repo / "README.md").read_text(encoding="utf-8")
    assert ft.REPORTS_MARKER[0] in text  # 旧格式被整体重建
    assert "[2026](reports/yearly/2026.md)" in text


def test_analysis_links_exclude_readme_and_underscore(repo):
    (repo / "analysis").mkdir()
    (repo / "analysis/README.md").write_text("# docs", encoding="utf-8")
    (repo / "analysis/_draft.md").write_text("# draft", encoding="utf-8")
    (repo / "analysis/2026-10-06-claude-mem.md").write_text("# a", encoding="utf-8")
    (repo / "analysis/2026-10-07-rea.md").write_text("# b", encoding="utf-8")
    assert [stem for stem, _ in ft.analysis_links()] == ["2026-10-07-rea", "2026-10-06-claude-mem"]
