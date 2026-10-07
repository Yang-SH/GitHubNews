#!/usr/bin/env python3
"""Fetch GitHub trending data and generate Markdown reports.

Periods:
- daily / monthly : scraped from github.com/trending (official trending list)
- yearly          : GitHub Search API (repos created in the last year,
                    sorted by stars) -- GitHub Trending has no yearly list.

Schedule behavior (Beijing time):
- daily report   : every day
- monthly report : additionally on day 1 of each month
- yearly report  : additionally on Jan 1 each year

Usage:
  python scripts/fetch_trending.py                # auto-detect by current date
  python scripts/fetch_trending.py --only daily   # force specific periods
"""
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests==2.34.2",
#     "beautifulsoup4==4.15.0",
# ]
# ///
# 注意：依赖版本需与 requirements.txt 保持一致

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BEIJING = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"
SNAPSHOT_README_CHARS = 2000  # README 摘录长度（快照存档深度，已确认）
YEARLY_DAYS = 365
YEARLY_PER_PAGE = 100
YEARLY_PAGES = 2
YEARLY_ATTEMPTS = 3
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
TRENDING_PAGES = 4  # 每页 25 条 × 4 页 ≈ Top 100。2026-10 实测官方页面只返回
# 12 条且忽略 page 参数（后续页与第 1 页重复，会被下方的去重逻辑自动截住），
# 保留翻页逻辑以兼容官方恢复分页的情况
README_HISTORY_LIMIT = {"daily": 30, "monthly": 24, "yearly": None}

UA = "GitHubNews-trending-bot/1.0 (+https://github.com)"

# 周期文案
TRENDING_META = {
    "daily": {"title": "每日", "source": "[GitHub Trending](https://github.com/trending)", "stars_col": "今日 Star"},
    "monthly": {"title": "每月", "source": "[GitHub Trending](https://github.com/trending?since=monthly)", "stars_col": "本月 Star"},
}


def now_bj() -> datetime:
    return datetime.now(BEIJING)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def get_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
    attempts: int = RETRY_ATTEMPTS,
) -> requests.Response:
    """对网络异常与 5xx 做指数退避重试；4xx 不重试，交由调用方处理。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code < 500:
                return resp
            last_error = requests.HTTPError(
                f"{resp.status_code} Server Error for url: {url}", response=resp
            )
        except requests.RequestException as e:
            last_error = e
        if attempt < attempts:
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            print(
                f"[WARN] GET {url} 失败（第 {attempt}/{attempts} 次）："
                f"{last_error}，{delay:.0f}s 后重试"
            )
            time.sleep(delay)
    raise last_error  # 循环走完必有失败记录


# ---------------------------------------------------------------------------
# 抓取：Trending 页面（日 / 月）
# ---------------------------------------------------------------------------

def parse_trending(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    items = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a")
        if not link or not link.get("href"):
            continue
        full_name = link["href"].strip("/")

        desc_el = row.select_one("p")
        lang_el = row.select_one('[itemprop="programmingLanguage"]')
        star_el = row.select_one("a[href$='/stargazers']")
        # 形如 "2,134 stars today" / "5,678 stars this week|month"
        m = re.search(
            r"([\d,]+)\s+stars\s+(?:today|this week|this month)",
            row.get_text(" ", strip=True),
        )

        items.append(
            {
                "name": full_name,
                "url": f"https://github.com/{full_name}",
                "desc": desc_el.get_text(" ", strip=True) if desc_el else "",
                "lang": lang_el.get_text(strip=True) if lang_el else "其他",
                "stars": star_el.get_text(" ", strip=True) if star_el else "-",
                "period_stars": f"+{m.group(1)}" if m else "-",
            }
        )
    return items


def scrape_trending(period: str, session: requests.Session) -> list[dict]:
    """抓取 Trending 榜单前 TRENDING_PAGES 页（≈ Top 100），按出现顺序去重合并。

    第 1 页为必需（解析为空会重试/报错）；后续页 best-effort：404、解析为空
    或整页与前文重复，都视为榜单不足一整页，正常结束。
    """
    items: dict[str, dict] = {}
    for page in range(1, TRENDING_PAGES + 1):
        url = f"https://github.com/trending?since={period}"
        if page > 1:
            url += f"&page={page}"

        if page == 1:
            # GitHub 偶发返回 200 但内容为空的变体页面，解析为空时重新抓取重试
            page_items = []
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                resp = get_with_retry(session, url)
                resp.raise_for_status()
                page_items = parse_trending(resp.text)
                if page_items:
                    break
                if attempt < RETRY_ATTEMPTS:
                    delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    print(
                        f"[WARN] Trending 页面解析为空（第 {attempt}/{RETRY_ATTEMPTS} 次）："
                        f"period={period}，{delay:.0f}s 后重试"
                    )
                    time.sleep(delay)
            if not page_items:
                raise RuntimeError(
                    f"Trending 页面解析结果为空（可能页面已改版）: period={period}"
                )
        else:
            resp = get_with_retry(session, url)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            page_items = parse_trending(resp.text)
            if not page_items:
                break

        before = len(items)
        for it in page_items:
            items.setdefault(it["name"], it)
        if page > 1 and len(items) == before:
            break
    return list(items.values())


# ---------------------------------------------------------------------------
# 抓取：年度（Search API，近一年创建且 Star 最高）
# ---------------------------------------------------------------------------

def _merge_candidate(candidates: dict[str, dict], repo: dict) -> None:
    """合并候选仓库；同一仓库多次命中时保留 Star 更高的一份（副本间数值有新旧）。"""
    count = repo.get("stargazers_count", 0)
    cur = candidates.get(repo["full_name"])
    if cur is None or count > cur["_stars"]:
        candidates[repo["full_name"]] = {
            "name": repo["full_name"],
            "url": repo["html_url"],
            "desc": (repo.get("description") or "").strip(),
            "lang": repo.get("language") or "其他",
            "stars": f"{count:,}",
            "created": repo.get("created_at", "")[:10],
            "_stars": count,
        }


def fetch_yearly(session: requests.Session, token: str | None) -> list[dict]:
    since = (now_bj() - timedelta(days=YEARLY_DAYS)).date().isoformat()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Search API 排序结果在不同索引副本间波动明显（同查询间隔几分钟的返回集
    # 可能完全不同），单次查询的 Top 25 偶然性大；多轮独立采样合并去重后
    # 本地按 Star 重排，覆盖率和稳定性都显著更好
    candidates: dict[str, dict] = {}
    for attempt in range(1, YEARLY_ATTEMPTS + 1):
        try:
            for page in range(1, YEARLY_PAGES + 1):
                resp = get_with_retry(
                    session, "https://api.github.com/search/repositories",
                    params={
                        "q": f"created:>{since}",
                        "sort": "stars",
                        "order": "desc",
                        "per_page": YEARLY_PER_PAGE,
                        "page": page,
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                for repo in resp.json().get("items", []):
                    _merge_candidate(candidates, repo)
        except requests.RequestException as e:
            if not candidates:
                raise
            # 首轮已有候选时，后续轮次失败只降级，不拖垮整份年报
            print(f"[WARN] 年报候选池第 {attempt} 轮获取失败，用已有候选继续: {e}")
            break

    ranked = sorted(candidates.values(), key=lambda it: it["_stars"], reverse=True)[:25]
    for it in ranked:
        del it["_stars"]
    if not ranked:
        raise RuntimeError("Search API 返回结果为空")
    return ranked


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def group_by_lang(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    first_pos: dict[str, int] = {}
    for i, it in enumerate(items):
        groups.setdefault(it["lang"], []).append(it)
        first_pos.setdefault(it["lang"], i)
    # 语言按其在总榜中的首次出现位置排序
    return sorted(groups.items(), key=lambda kv: first_pos[kv[0]])


def render_table(rows: list[list[str]]) -> list[str]:
    header = "| # | 仓库 | 描述 | 语言 | 总 Star | 周期增长 |"
    sep = "|---|------|------|------|---------|----------|"
    lines = [header, sep]
    for rank, name, url, desc, lang, stars, extra in rows:
        desc = desc.replace("|", "\\|")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        lines.append(f"| {rank} | [{name}]({url}) | {desc} | {lang} | {stars} | {extra} |")
    return lines


def render_report(
    kind: str,
    items: list[dict],
    meta: dict,
    generated_at: datetime,
) -> str:
    date_str = (
        generated_at.strftime("%Y-%m") if kind == "monthly" else generated_at.strftime("%Y-%m-%d")
    )
    title = f"GitHub {meta['title']}热点 — {date_str}"
    global_rank = {it["name"]: i for i, it in enumerate(items, 1)}

    lines = [
        f"# {title}",
        "",
        f"> 数据来源：{meta['source']} ｜ 抓取时间：{generated_at.strftime('%Y-%m-%d %H:%M')}"
        f"（北京时间）｜ 表中 # 为总榜排名",
        "",
    ]

    for lang, group in group_by_lang(items):
        lines.append(f"## {lang}")
        lines.append("")
        rows = [
            [global_rank[it["name"]], it["name"], it["url"], it["desc"], it["lang"],
             it["stars"], it["period_stars"]]
            for it in group
        ]
        lines.extend(render_table(rows))
        lines.append("")

    return "\n".join(lines)


def render_report_yearly(items: list[dict], generated_at: datetime) -> str:
    since = (generated_at - timedelta(days=YEARLY_DAYS)).strftime("%Y-%m-%d")
    global_rank = {it["name"]: i for i, it in enumerate(items, 1)}
    lines = [
        f"# GitHub 年度热点 — {generated_at.year}",
        "",
        f"> 数据来源：[GitHub Search API](https://api.github.com/search/repositories) ｜ "
        f"口径：{since} 之后创建且 Star 数最高的新项目 Top 25 ｜ "
        f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M')}（北京时间）｜ 表中 # 为总榜排名"
        f"（Search API 索引存在波动，榜单为查询时点快照）",
        "",
    ]
    for lang, group in group_by_lang(items):
        lines.append(f"## {lang}")
        lines.append("")
        lines.append("| # | 仓库 | 描述 | 语言 | 总 Star | 创建日期 |")
        lines.append("|---|------|------|------|---------|----------|")
        for it in group:
            desc = it["desc"].replace("|", "\\|")
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(
                f"| {global_rank[it['name']]} | [{it['name']}]({it['url']}) | {desc} "
                f"| {it['lang']} | {it['stars']} | {it['created']} |"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# README 索引
# ---------------------------------------------------------------------------

def report_links(kind: str) -> list[tuple[str, Path]]:
    base = REPORTS_DIR / kind
    if not base.exists():
        return []
    files = sorted(base.glob("*.md"), reverse=True)
    limit = README_HISTORY_LIMIT[kind]
    return [(f.stem, f) for f in files[:limit]] if limit else [(f.stem, f) for f in files]


def analysis_links() -> list[tuple[str, Path]]:
    """analysis/ 下的分析报告（排除 README 与 _ 开头的辅助文件），按文件名倒序。"""
    base = ROOT / "analysis"
    if not base.exists():
        return []
    files = sorted(
        (p for p in base.glob("*.md") if p.name != "README.md" and not p.name.startswith("_")),
        reverse=True,
    )
    return [(p.stem, p) for p in files]


REPORTS_MARKER = ("<!-- reports:index:start -->", "<!-- reports:index:end -->")
ANALYSIS_MARKER = ("<!-- analysis:index:start -->", "<!-- analysis:index:end -->")


def render_intro() -> list[str]:
    return [
        "# GitHubNews — GitHub 热点项目追踪",
        "",
        "由 GitHub Actions 每日定时抓取 GitHub 热点项目，自动提交榜单报告（`reports/`）与数据快照（`data/`，含 README 摘录）；本地 agent 基于快照生成问题与场景分析，产出 `analysis/` 下的分析报告（人工审核后入库）。",
        "",
        "| 报告 | 生成频率 | 口径 |",
        "|------|----------|------|",
        "| 日报 | 每天 06:30（北京时间） | [GitHub Trending](https://github.com/trending) 官方口径（daily，2026-10 起官方页面每日仅 12 条） |",
        "| 月报 | 每月 1 日 | GitHub Trending 官方口径（monthly） |",
        "| 年报 | 每年 1 月 1 日 | Search API：近一年创建且 Star 最高的新项目 Top 25 |",
        "| 分析 | 持续更新 | 基于 `data/` 快照的问题与场景分析（人工审核入库） |",
        "| 历史回填 | 一次性 | GH Archive WatchEvent：每日新增 Star Top 100（2025-01-01 ~ 2026-03-31，见 `analysis/README.md`） |",
    ]


def render_reports_index() -> list[str]:
    kind_titles = {"daily": "日报", "monthly": "月报", "yearly": "年报"}
    lines = ["## 最新报告", ""]
    for kind in ("daily", "monthly", "yearly"):
        links = report_links(kind)
        if links:
            stem, path = links[0]
            lines.append(f"- **{kind_titles[kind]}**：[{stem}]({path.relative_to(ROOT).as_posix()})")
        else:
            lines.append(f"- **{kind_titles[kind]}**：暂无")
    lines += ["", "## 历史报告", ""]
    for kind in ("daily", "monthly", "yearly"):
        lines.append(f"### {kind_titles[kind]}（最新在前）")
        lines.append("")
        links = report_links(kind)
        if links:
            lines.extend(
                f"- [{stem}]({path.relative_to(ROOT).as_posix()})" for stem, path in links
            )
        else:
            lines.append("- 暂无")
        lines.append("")
    return lines


def render_analysis_index() -> list[str]:
    links = analysis_links()
    lines = ["## 项目分析", ""]
    if links:
        lines.extend(f"- [{stem}]({path.relative_to(ROOT).as_posix()})" for stem, path in links)
    else:
        lines.append(
            "暂无。基于每日快照的分析经人工审核入库后，会自动列在这里；"
            "流程与模板见 [analysis/README.md](analysis/README.md)。"
        )
    return lines


def _splice_block(lines: list[str], start: str, end: str, block: list[str]) -> bool:
    try:
        i = lines.index(start)
        j = lines.index(end, i + 1)
    except ValueError:
        return False
    lines[i + 1 : j] = block
    return True


def update_readme(generated_at: datetime) -> None:
    """只更新标记区块内的报告/分析索引；区块外的内容（如手写的开发说明）原样保留。"""
    readme = ROOT / "README.md"
    existing = readme.read_text(encoding="utf-8") if readme.exists() else ""

    if existing:
        lines = existing.rstrip("\n").split("\n")
        ok_reports = _splice_block(lines, *REPORTS_MARKER, render_reports_index())
        ok_analysis = _splice_block(lines, *ANALYSIS_MARKER, render_analysis_index())
        if ok_reports and ok_analysis:
            readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return

    # 首次生成或标记缺失时整体重建
    lines = (
        render_intro()
        + [""]
        + [REPORTS_MARKER[0]]
        + render_reports_index()
        + [REPORTS_MARKER[1], ""]
        + [ANALYSIS_MARKER[0]]
        + render_analysis_index()
        + [ANALYSIS_MARKER[1], ""]
    )
    readme.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 数据快照（供本地 agent 分析消费）
# ---------------------------------------------------------------------------

def fetch_readme_excerpt(
    session: requests.Session,
    full_name: str,
    token: str | None,
) -> str:
    headers = {"Accept": "application/vnd.github.raw"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        # 摘录是锦上添花，失败只降级不中断；重试次数比主流程少，避免拖长整体运行
        r = get_with_retry(
            session,
            f"https://api.github.com/repos/{full_name}/readme",
            headers=headers,
            attempts=2,
        )
        if r.status_code == 200:
            return r.text[:SNAPSHOT_README_CHARS]
        hint = (
            "（可设置 GITHUB_TOKEN 环境变量后重试）"
            if r.status_code in (401, 403) and not token
            else ""
        )
        print(f"[WARN] README 摘录获取失败: {full_name} (HTTP {r.status_code}){hint}")
    except requests.RequestException as e:
        print(f"[WARN] README 摘录获取失败: {full_name} ({type(e).__name__}: {str(e)[:120]})")
    return ""


def enrich_readme(
    items: list[dict],
    session: requests.Session,
    token: str | None,
) -> list[dict]:
    out = []
    for it in items:
        it2 = dict(it)
        it2["readme"] = fetch_readme_excerpt(session, it2["name"], token)
        out.append(it2)
    return out


def write_snapshot(
    periods_data: dict[str, list[dict]],
    generated_at: datetime,
) -> None:
    """把当天所有周期的榜单数据（含 README 摘录）写入 data/YYYY-MM-DD.json。"""
    snap: dict = {
        "date": f"{generated_at:%Y-%m-%d}",
        "fetched_at": generated_at.isoformat(),
    }
    for period, items in periods_data.items():
        snap[period] = items

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{generated_at:%Y-%m-%d}.json"
    out.write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    total = sum(len(v) for v in periods_data.values())
    with_readme = sum(
        1 for items in periods_data.values() for it in items if it.get("readme")
    )
    print(
        f"[OK] snapshot -> data/{generated_at:%Y-%m-%d}.json "
        f"({total} repos, README 摘录 {with_readme}/{total})"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def detect_periods() -> list[str]:
    now = now_bj()
    periods = ["daily"]
    if now.day == 1:
        periods.append("monthly")
    if (now.month, now.day) == (1, 1):
        periods.append("yearly")
    return periods


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch GitHub trending and write reports")
    parser.add_argument(
        "--only",
        choices=["daily", "monthly", "yearly"],
        action="append",
        help="只生成指定周期（可多次使用）；缺省时按当天日期自动判断",
    )
    args = parser.parse_args()

    generated_at = now_bj()
    periods = args.only if args.only else detect_periods()
    token = os.environ.get("GITHUB_TOKEN") or None
    session = make_session()

    periods_data: dict[str, list[dict]] = {}

    for period in periods:
        if period == "yearly":
            items = fetch_yearly(session, token)
            content = render_report_yearly(items, generated_at)
            out = REPORTS_DIR / "yearly" / f"{generated_at.year}.md"
        else:
            items = scrape_trending(period, session)
            content = render_report(period, items, TRENDING_META[period], generated_at)
            if period == "daily":
                out = REPORTS_DIR / "daily" / f"{generated_at:%Y-%m-%d}.md"
            else:
                out = REPORTS_DIR / "monthly" / f"{generated_at:%Y-%m}.md"

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        print(f"[OK] {period}: {len(items)} repos -> {out.relative_to(ROOT)}")
        periods_data[period] = items

    # 数据快照（README 摘录 + 榜单元数据），供本地 agent 分析
    enriched = {
        p: enrich_readme(items, session, token) for p, items in periods_data.items()
    }
    write_snapshot(enriched, generated_at)

    update_readme(generated_at)
    print(f"[OK] README.md updated ({generated_at:%Y-%m-%d %H:%M} CST)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
