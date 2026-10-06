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
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
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
    url = f"https://github.com/trending?since={period}"
    # GitHub 偶发返回 200 但内容为空的变体页面，解析为空时同样重试
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        resp = get_with_retry(session, url)
        resp.raise_for_status()
        items = parse_trending(resp.text)
        if items:
            return items
        if attempt < RETRY_ATTEMPTS:
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            print(
                f"[WARN] Trending 页面解析为空（第 {attempt}/{RETRY_ATTEMPTS} 次）："
                f"period={period}，{delay:.0f}s 后重试"
            )
            time.sleep(delay)
    raise RuntimeError(f"Trending 页面解析结果为空（可能页面已改版）: period={period}")


# ---------------------------------------------------------------------------
# 抓取：年度（Search API，近一年创建且 Star 最高）
# ---------------------------------------------------------------------------

def fetch_yearly(session: requests.Session, token: str | None) -> list[dict]:
    since = (now_bj() - timedelta(days=YEARLY_DAYS)).date().isoformat()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = get_with_retry(
        session,
        "https://api.github.com/search/repositories",
        params={
            "q": f"created:>{since}",
            "sort": "stars",
            "order": "desc",
            "per_page": 25,
            "page": 1,
        },
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()

    items = []
    for repo in data.get("items", []):
        items.append(
            {
                "name": repo["full_name"],
                "url": repo["html_url"],
                "desc": (repo.get("description") or "").strip(),
                "lang": repo.get("language") or "其他",
                "stars": f"{repo.get('stargazers_count', 0):,}",
                "created": repo.get("created_at", "")[:10],
            }
        )

    if not items:
        raise RuntimeError("Search API 返回结果为空")
    return items


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def group_by_lang(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for it in items:
        groups.setdefault(it["lang"], []).append(it)
    # 语言按组内最高排名排序（即保持榜单上的出现顺序）
    return sorted(groups.items(), key=lambda kv: items.index(kv[1][0]))


def render_table(rows: list[list[str]]) -> list[str]:
    header = "| # | 仓库 | 描述 | 语言 | 总 Star | 周期增长 |"
    sep = "|---|------|------|------|---------|----------|"
    lines = [header, sep]
    for i, (name, url, desc, lang, stars, extra) in enumerate(rows, 1):
        desc = desc.replace("|", "\\|")
        if len(desc) > 80:
            desc = desc[:77] + "..."
        lines.append(f"| {i} | [{name}]({url}) | {desc} | {lang} | {stars} | {extra} |")
    return lines


def render_report(
    kind: str,
    items: list[dict],
    meta: dict,
    generated_at: datetime,
) -> str:
    date_str = generated_at.strftime("%Y-%m-%d")
    title = f"GitHub {meta['title']}热点 — {date_str}"

    lines = [
        f"# {title}",
        "",
        f"> 数据来源：{meta['source']} ｜ 抓取时间：{generated_at.strftime('%Y-%m-%d %H:%M')}（北京时间）",
        "",
    ]

    for lang, group in group_by_lang(items):
        lines.append(f"## {lang}")
        lines.append("")
        if kind == "yearly":
            rows = [
                [it["name"], it["url"], it["desc"], it["lang"], it["stars"], it["created"]]
                for it in group
            ]
        else:
            rows = [
                [it["name"], it["url"], it["desc"], it["lang"], it["stars"], it["period_stars"]]
                for it in group
            ]
        lines.extend(render_table(rows))
        lines.append("")

    return "\n".join(lines)


YEARLY_HEADER_OVERRIDE = ("| # | 仓库 | 描述 | 语言 | 总 Star | 创建日期 |",
                          "|---|------|------|------|---------|----------|")


def render_report_yearly(items: list[dict], generated_at: datetime) -> str:
    since = (generated_at - timedelta(days=YEARLY_DAYS)).strftime("%Y-%m-%d")
    lines = [
        f"# GitHub 年度热点 — {generated_at.year}",
        "",
        f"> 数据来源：[GitHub Search API](https://api.github.com/search/repositories) ｜ "
        f"口径：{since} 之后创建且 Star 数最高的新项目 Top 25 ｜ "
        f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M')}（北京时间）",
        "",
    ]
    for lang, group in group_by_lang(items):
        lines.append(f"## {lang}")
        lines.append("")
        lines.append("| # | 仓库 | 描述 | 语言 | 总 Star | 创建日期 |")
        lines.append("|---|------|------|------|---------|----------|")
        for i, it in enumerate(group, 1):
            desc = it["desc"].replace("|", "\\|")
            if len(desc) > 80:
                desc = desc[:77] + "..."
            lines.append(
                f"| {i} | [{it['name']}]({it['url']}) | {desc} | {it['lang']} "
                f"| {it['stars']} | {it['created']} |"
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


def update_readme(generated_at: datetime) -> None:
    kind_titles = {
        "daily": ("日报", "每天 08:00（北京时间）"),
        "monthly": ("月报", "每月 1 日随日报一并生成"),
        "yearly": ("年报", "每年 1 月 1 日生成（近一年创建且 Star 最高的新项目 Top 25）"),
    }

    lines = [
        "# GitHubNews — GitHub 热点项目追踪",
        "",
        "由 GitHub Actions 每日定时抓取 GitHub 热点项目，自动提交榜单报告（`reports/`）与数据快照（`data/`，含 README 摘录）；本地 agent 基于快照生成问题与场景分析，产出 `analysis/` 下的分析报告（人工审核后入库）。",
        "",
        "| 报告 | 生成频率 | 口径 |",
        "|------|----------|------|",
        "| 日报 | 每天 08:00（北京时间） | [GitHub Trending](https://github.com/trending) 官方口径（daily） |",
        "| 月报 | 每月 1 日 | GitHub Trending 官方口径（monthly） |",
        "| 年报 | 每年 1 月 1 日 | Search API：近一年创建且 Star 最高的新项目 Top 25 |",
        "",
        "## 最新报告",
        "",
    ]

    for kind in ("daily", "monthly", "yearly"):
        title, _ = kind_titles[kind]
        links = report_links(kind)
        if links:
            stem, path = links[0]
            rel = path.relative_to(ROOT).as_posix()
            lines.append(f"- **{title}**：[{stem}]({rel})")
        else:
            lines.append(f"- **{title}**：暂无")

    lines += ["", "## 历史报告", ""]
    for kind in ("daily", "monthly", "yearly"):
        title, _ = kind_titles[kind]
        lines.append(f"### {title}（最新在前）")
        lines.append("")
        links = report_links(kind)
        if links:
            for stem, path in links:
                rel = path.relative_to(ROOT).as_posix()
                lines.append(f"- [{stem}]({rel})")
        else:
            lines.append("- 暂无")
        lines.append("")

    (ROOT / "README.md").write_text("\n".join(lines), encoding="utf-8")


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
