#!/usr/bin/env python3
"""Backfill historical daily trending snapshots into data/ (top N per day).

GitHub Trending 页面不支持历史查询（只能抓"今天"），本脚本改用 GH Archive
的 WatchEvent（Star 事件，action 恒为 started，无 unstar 噪声）在 ClickHouse
公共数据集（play.clickhouse.com 的 github_events 表）上按 UTC 天重算
"当日新增 Star Top N"，并按 data/YYYY-MM-DD.json 快照格式回填历史数据。

口径差异（与 fetch_trending.py 抓取的官方 Trending 对比，详见
analysis/README.md）：
- 排序口径为"当日新增 Star 数"；官方 Trending 由 GitHub 私有算法排序
  （综合 Star 增长、浏览量等），两者高度相关但不完全一致；
- 天边界为 UTC（created_at 的原始时区）；
- 总 Star 为"当日结束时"的 WatchEvent 累计推算值（GH Archive 自 2011-02
  起有数据）。注意 GitHub 公共事件流自 2025 年起渐进丢失 Star 事件
  （2025 年约捕获 60%-90%，此后持续恶化），该值为系统性偏低的上界不确定
  的下界估计，仅供参考；
- desc / lang / readme 属于时点数据，历史不可得，回填条目相应置空。

数据可靠性（依据与 GitHub API 真实星数的对照标定）：
- 2025-01-01 ~ 2025-10-31：质量 good（捕获率约 60%-90%，排序忠实）；
- 2025-11-01 ~ 2026-03-31：质量 degraded（捕获率约 15%-60%，排序大体可信）；
- 2026-04-01 之后：捕获率 < 15%，榜单失真（真正的现象级项目几乎完全
  缺失），默认不回填；确需时可用 --include-unreliable 强制。

Usage:
  uv run scripts/backfill_trending.py                    # 2025-01-01 → 可信截止日
  uv run scripts/backfill_trending.py --start 2025-06-01 --end 2025-06-30
  uv run scripts/backfill_trending.py --top-n 50

已有 data/YYYY-MM-DD.json 的日期默认跳过（幂等，可安全重跑）；
当日（UTC 未结束）不参与回填，避免写入不完整的半日数据。
"""
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests==2.34.2",
# ]
# ///

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BEIJING = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

PLAYGROUND_URL = "https://play.clickhouse.com/"
PLAYGROUND_USER = "explorer"
DEFAULT_START = date(2025, 1, 1)
DEFAULT_TOP_N = 100
HTTP_TIMEOUT = 300  # 标量超时（连接与读均为 300s）。重聚合查询首个字节可能
# 迟到十几秒，元组 (connect, read) 在部分 requests 版本下实际读超时偏短，勿改回元组
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 3.0
IN_CHUNK = 1000  # ClickHouse 默认 max_query_size 256KiB，IN 列表需分批；
# 分批越小单块响应越小，弱网下重试代价越低

# 事件流 Star 捕获率标定结论（对照 GitHub API 真实星数，2026-10-07）：
# 2025 年内 60%-90% 且排序忠实；2025-11 起约 15%-60%；2026-04 后 <15%，
# 现象级项目（如 tester-army/e2e 真实 +1700/日）在事件流中仅剩个位数事件，
# 榜单失真。RELIABILITY_CUTOFF 之后默认不回填。
RELIABILITY_CUTOFF = date(2026, 3, 31)
GOOD_UNTIL = date(2025, 10, 31)


def quality_for(day: date) -> str:
    return "good" if day <= GOOD_UNTIL else "degraded"


def now_bj() -> datetime:
    return datetime.now(BEIJING)


# ---------------------------------------------------------------------------
# ClickHouse 查询
# ---------------------------------------------------------------------------

def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def post_query(session: requests.Session, sql: str) -> requests.Response:
    """执行只读查询并返回流式响应；网络错误与 5xx 指数退避重试。"""
    last_error: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.post(
                PLAYGROUND_URL,
                params={"user": PLAYGROUND_USER},
                data=sql.encode("utf-8"),
                timeout=HTTP_TIMEOUT,
                stream=True,
            )
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            last_error = requests.HTTPError(
                f"{resp.status_code} Server Error for url: {PLAYGROUND_URL}",
                response=resp,
            )
        except requests.RequestException as e:
            last_error = e
        if attempt < RETRY_ATTEMPTS:
            delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
            print(f"[WARN] 查询失败（第 {attempt}/{RETRY_ATTEMPTS} 次）：{last_error}，{delay:.0f}s 后重试")
            time.sleep(delay)
    raise last_error  # 循环走完必有失败记录


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """把 [start, end] 按自然月切块，返回各块的 (块首日, 块末日)（含端点）。"""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        chunks.append((cur, min(end, nxt - timedelta(days=1))))
        cur = nxt
    return chunks


def fetch_month_top(
    session: requests.Session,
    first: date,
    last: date,
    top_n: int,
) -> dict[str, list[tuple[str, int]]]:
    """返回 {日期: [(repo, 当日新增Star), ...]}（按新增 Star 降序，每日至多 top_n 个）。"""
    sql = f"""
    SELECT day, repo_name, stars
    FROM
    (
        SELECT toDate(created_at) AS day, repo_name, count() AS stars
        FROM github_events
        WHERE event_type = 'WatchEvent'
          AND created_at >= '{first:%Y-%m-%d 00:00:00}'
          AND created_at <  '{last + timedelta(days=1):%Y-%m-%d 00:00:00}'
        GROUP BY day, repo_name
    )
    ORDER BY day ASC, stars DESC, repo_name ASC
    LIMIT {top_n} BY day
    FORMAT TSV
    """
    resp = post_query(session, sql)
    top: dict[str, list[tuple[str, int]]] = {}
    with resp:
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            day, repo, stars = line.split("\t")
            top.setdefault(day, []).append((repo, int(stars)))
    return top


def _repo_in_chunks(repos: list[str]) -> list[str]:
    """把仓库名列表切成多段 IN 子句（单段超出 max_query_size 会 400）。"""
    return [
        ",".join(f"'{_escape(r)}'" for r in repos[i : i + IN_CHUNK])
        for i in range(0, len(repos), IN_CHUNK)
    ]


def fetch_baseline(
    session: requests.Session,
    repos: list[str],
    before: datetime,
) -> dict[str, int]:
    """区间开始前的历史 Star 累计（WatchEvent 计数，GH Archive 自 2011-02 起）。"""
    if not repos:
        return {}
    baseline: dict[str, int] = {}
    for in_list in _repo_in_chunks(repos):
        sql = f"""
        SELECT repo_name, count()
        FROM github_events
        WHERE event_type = 'WatchEvent'
          AND created_at < '{before:%Y-%m-%d %H:%M:%S}'
          AND repo_name IN ({in_list})
        GROUP BY repo_name
        FORMAT TSV
        """
        resp = post_query(session, sql)
        with resp:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                repo, stars = line.split("\t")
                baseline[repo] = int(stars)
    return baseline


def fetch_daily_counts(
    session: requests.Session,
    repos: list[str],
    start_dt: datetime,
    end_dt: datetime,
) -> dict[str, dict[str, int]]:
    """取区间内所有仓库的逐日事件计数网格 {day: {repo: count}}。

    服务端只做 GROUP BY（数据量为登榜仓库数 × 有事件的天数，单响应可控），
    窗口累计移到客户端 resolve_totals 完成；IN 列表分批规避 max_query_size。
    """
    grid: dict[str, dict[str, int]] = {}
    if not repos:
        return grid
    for in_list in _repo_in_chunks(repos):
        sql = f"""
        SELECT toDate(created_at) AS day, repo_name, count() AS cnt
        FROM github_events
        WHERE event_type = 'WatchEvent'
          AND created_at >= '{start_dt:%Y-%m-%d %H:%M:%S}'
          AND created_at <  '{end_dt:%Y-%m-%d %H:%M:%S}'
          AND repo_name IN ({in_list})
        GROUP BY day, repo_name
        FORMAT TSV
        """
        resp = post_query(session, sql)
        with resp:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                day, repo, cnt = line.split("\t")
                grid.setdefault(day, {})[repo] = int(cnt)
    return grid


def resolve_totals(
    baseline: dict[str, int],
    grid: dict[str, dict[str, int]],
    top_by_day: dict[str, list[tuple[str, int]]],
) -> dict[tuple[str, str], int]:
    """把基线与逐日计数网格合成每个 (repo, 登榜日) 的"当日结束时总 Star"。

    按日期序扫一遍网格，运行累计（基线起算）；登榜日当天结束时取一次快照，
    语义与官方快照"抓取时总 Star 含今日新增"一致。网格缺行（当日 0 新增
    事件）不影响累计——保持上一次的运行值即可。
    """
    totals: dict[tuple[str, str], int] = {}
    running: dict[str, int] = dict(baseline)
    for day in sorted(set(grid) | set(top_by_day)):
        for repo, cnt in grid.get(day, {}).items():
            running[repo] = running.get(repo, 0) + cnt
        for repo, _ in top_by_day.get(day, []):
            totals[(repo, day)] = running.get(repo, 0)
    return totals


def fetch_cumulative_totals(
    session: requests.Session,
    repos: list[str],
    top_by_day: dict[str, list[tuple[str, int]]],
    baseline: dict[str, int],
    start: datetime,
    end_exclusive: datetime,
) -> dict[tuple[str, str], int]:
    """计算每个 (repo, 登榜日) 在"当日结束时"的总 Star。

    按月推进：对第 M 月有登榜日的仓库，在服务端用窗口函数累计
    [区间开始, M 月末] 的逐日计数，再筛选出该月的登榜日对返回。
    单次响应只有该月的登榜日行（约百 KB），弱网下可靠；分月独立重试。
    事件流有丢失，结果为下界估计（当日新增计入当日总 Star，与官方快照
    "抓取时总 Star 含今日新增"的语义一致）。
    """
    top_days: dict[str, list[str]] = {}
    for day, rows in top_by_day.items():
        for repo, _ in rows:
            top_days.setdefault(repo, []).append(day)
    all_pairs = {(repo, day) for repo, days in top_days.items() for day in days}

    totals: dict[tuple[str, str], int] = {}
    months = month_chunks(start.date(), end_exclusive.date() - timedelta(days=1))
    for m_first, m_last in months:
        pairs = sorted(
            (repo, day)
            for repo, days in top_days.items()
            for day in days
            if m_first.isoformat() <= day <= m_last.isoformat()
        )
        if not pairs:
            continue
        repos_m = sorted({repo for repo, _ in pairs})
        in_repos = ",".join(f"'{_escape(r)}'" for r in repos_m)
        in_pairs = ",".join(f"('{_escape(r)}',toDate('{d}'))" for r, d in pairs)
        sql = f"""
        SELECT repo_name, day, cum
        FROM
        (
            SELECT day, repo_name,
                   sum(cnt) OVER (PARTITION BY repo_name ORDER BY day ASC) AS cum
            FROM
            (
                SELECT toDate(created_at) AS day, repo_name, count() AS cnt
                FROM github_events
                WHERE event_type = 'WatchEvent'
                  AND created_at >= '{start:%Y-%m-%d %H:%M:%S}'
                  AND created_at <  '{m_last + timedelta(days=1):%Y-%m-%d %H:%M:%S}'
                  AND repo_name IN ({in_repos})
                GROUP BY day, repo_name
            )
        )
        WHERE (repo_name, day) IN ({in_pairs})
        FORMAT TSV
        """
        last_error: Exception | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                resp = post_query(session, sql)
                with resp:
                    for line in resp.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        repo, day, cum = line.split("\t")
                        totals[(repo, day)] = baseline.get(repo, 0) + int(cum)
                last_error = None
                break
            except requests.RequestException as e:
                last_error = e
                if attempt < RETRY_ATTEMPTS:
                    delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    print(f"[WARN] {m_first} 月累计查询失败（第 {attempt}/{RETRY_ATTEMPTS} 次）：{e}，{delay:.0f}s 后重试")
                    time.sleep(delay)
        if last_error is not None:
            raise last_error
        print(f"[INFO] {m_first:%Y-%m} 累计完成（{len(pairs)} 条）")
    # 理论上每个登榜日对都有窗口行；防御性跳过缺失（build_items 会记 "-"）
    return {k: v for k, v in totals.items() if k in all_pairs}


def _retry_call(fn, *args, **kwargs):
    """post_query 只重试初始请求；流式读取中途断连在此兜底重试整个查询。"""
    last_error: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except requests.RequestException as e:
            last_error = e
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1)
                print(f"[WARN] {fn.__name__} 失败（第 {attempt}/{RETRY_ATTEMPTS} 次）：{e}，{delay:.0f}s 后重试")
                time.sleep(delay)
    raise last_error  # 循环走完必有失败记录


# ---------------------------------------------------------------------------
# 组装快照
# ---------------------------------------------------------------------------

def build_items(
    day_rows: list[tuple[str, int]],
    day: str,
    totals: dict[tuple[str, str], int],
) -> list[dict]:
    """把一天的榜单组装成与官方快照同构的条目列表（rank 为新增字段）。"""
    items = []
    for rank, (repo, gained) in enumerate(day_rows, 1):
        total = totals.get((repo, day))
        items.append(
            {
                "rank": rank,
                "name": repo,
                "url": f"https://github.com/{repo}",
                "desc": "",
                "lang": "",
                "stars": f"{total:,}" if total is not None else "-",
                "period_stars": f"+{gained:,}",
            }
        )
    return items


def snapshot_doc(day: str, items: list[dict], fetched_at: datetime) -> dict:
    return {
        "date": day,
        "fetched_at": fetched_at.isoformat(),
        "source": "gharchive-watchevent",
        "quality": quality_for(date.fromisoformat(day)),
        "daily": items,
    }


def write_snapshot(day: str, doc: dict) -> Path:
    out = DATA_DIR / f"{day}.json"
    out.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def default_end(today_utc: date) -> date:
    """默认回填到昨天（UTC）：当天数据尚未累积完整，不回填。"""
    return today_utc - timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill daily top-N trending snapshots from GH Archive")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START,
                        help="起始日期（含，UTC，默认 2025-01-01）")
    parser.add_argument("--end", type=date.fromisoformat, default=None,
                        help="结束日期（含，UTC，默认昨天，且不晚于可靠性截止日）")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                        help=f"每日取前 N 名（默认 {DEFAULT_TOP_N}）")
    parser.add_argument("--include-unreliable", action="store_true",
                        help="允许回填可靠性截止日（2026-03-31）之后的失真数据")
    args = parser.parse_args()

    end = args.end or default_end(datetime.now(timezone.utc).date())
    if not args.include_unreliable and end > RELIABILITY_CUTOFF:
        print(
            f"[INFO] 事件流 Star 捕获率自 2026-04 起过低（<15%，榜单失真），"
            f"结束日期从 {end} 收紧到 {RELIABILITY_CUTOFF}；强制回填请加 --include-unreliable"
        )
        end = RELIABILITY_CUTOFF
    if args.start > end:
        print(f"[ERROR] --start ({args.start}) 晚于 --end ({end})")
        return 2

    pending = {
        (args.start + timedelta(days=i)).isoformat()
        for i in range((end - args.start).days + 1)
    } - {p.stem for p in DATA_DIR.glob("*.json")}
    if not pending:
        print(f"[OK] {args.start} ~ {end} 的快照均已存在，无需回填")
        return 0
    print(f"[INFO] 待回填 {len(pending)} 天（{min(pending)} ~ {max(pending)}），top_n={args.top_n}")

    session = requests.Session()
    fetched_at = now_bj()

    # 阶段 1：按自然月取每日 Top N
    top_by_day: dict[str, list[tuple[str, int]]] = {}
    chunks = [c for c in month_chunks(args.start, end)]
    for i, (first, last) in enumerate(chunks, 1):
        top = _retry_call(fetch_month_top, session, first, last, args.top_n)
        top_by_day.update(top)
        print(f"[INFO] ({i}/{len(chunks)}) {first} ~ {last}: {len(top)} 天")

    top_by_day = {d: rows for d, rows in top_by_day.items() if d in pending}
    if not top_by_day:
        print("[ERROR] 未取得任何榜单数据")
        return 1

    # 阶段 2：为"当日结束时总 Star"做服务端窗口累计（失败则整体中止，不写半成品）
    repos = sorted({repo for rows in top_by_day.values() for repo, _ in rows})
    try:
        start_dt = datetime(args.start.year, args.start.month, args.start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        baseline = _retry_call(fetch_baseline, session, repos, start_dt)
        print(f"[INFO] 基线仓库 {len(baseline)}/{len(repos)}（区间开始前有 Star 记录）")
        grid = fetch_daily_counts(session, repos, start_dt, end_dt)
        totals = resolve_totals(baseline, grid, top_by_day)
        print(f"[INFO] 总 Star 推算完成：{len(totals)} 条仓库日记录")
    except requests.RequestException as e:
        # 写出缺总 Star 的半成品存档不如不写：失败则整体中止，直接重跑即可
        print(f"[ERROR] 总 Star 推算失败（网络重试后仍失败），本次不写入: {e}")
        return 1

    # 阶段 3：写快照
    written = 0
    for day in sorted(top_by_day):
        items = build_items(top_by_day[day], day, totals)
        out = write_snapshot(day, snapshot_doc(day, items, fetched_at))
        written += 1
        print(f"[OK] data/{out.name} ({len(items)} repos)")

    print(f"[DONE] 回填完成：{written} 天，仓库 {len(repos)} 个，写入 {DATA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
