#!/usr/bin/env python3
"""汇总榜单快照与仓库 Star 观测，生成 dashboard/summary.json。

只依赖标准库，trending 工作流在提交报告前运行；本地可手动执行：

    uv run python scripts/build_dashboard.py

summary.json 按来源隔离榜单聚合，并包含官方榜单比较与 Star 时间序列；
每天历史回填展示可保留前 N 条，完整榜单仍以 data/YYYY-MM-DD.json 为准。
看板页（dashboard/index.html）通过相对路径加载 summary.json。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(ROOT, "dashboard", "summary.json")


def to_int(raw: object) -> int | None:
    """解析 '1,568' / '+734' 这类格式化数字，失败返回 None。"""
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace(",", "").lstrip("+")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def compact_desc(raw: object, limit: int) -> str:
    """压成单行短摘要，供榜单列表与悬浮提示使用。"""
    if not isinstance(raw, str):
        return ""
    text = " ".join(raw.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def load_day(path: str) -> dict | None:
    """读取单个快照，返回规范化后的天记录；坏文件告警并跳过。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] 跳过损坏的快照 {os.path.basename(path)}: {exc}", file=sys.stderr)
        return None

    items = data.get("daily")
    if not isinstance(items, list) or not items:
        print(f"[WARN] 跳过空快照 {os.path.basename(path)}", file=sys.stderr)
        return None

    source = data.get("source") or "official"
    normalized = []
    lang_counts: dict[str, int] = {}
    for it in items:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        lang = (it.get("lang") or "").strip()
        if lang:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
        normalized.append(
            {
                "n": it["name"],
                "s": to_int(it.get("stars")),
                "p": to_int(it.get("period_stars")),
                "l": lang,
                "d": compact_desc(it.get("desc"), 90),
            }
        )
    if not normalized:
        print(f"[WARN] 跳过无有效条目的快照 {os.path.basename(path)}", file=sys.stderr)
        return None

    periods = [it["p"] for it in normalized if it["p"] is not None]
    return {
        "src": source,
        "q": data.get("quality"),  # 官方快照无此字段
        "n": len(normalized),
        "top": max(periods) if periods else None,
        "sum": sum(periods) if periods else None,
        "lang_counts": lang_counts,
        # 榜单顺序即排名；回填数据自带 rank，官方快照由顺序隐含
        "items": normalized,
    }


def cohort_key(source: str, quality: str | None) -> str:
    """Keep sources and backfill quality levels in separate dashboard series."""
    if source == "official":
        return "official"
    if source == "gharchive-watchevent":
        return f"gharchive-{quality or 'ungraded'}"
    return f"{source}-{quality or 'ungraded'}"


def new_cohort(source: str, quality: str | None) -> dict:
    return {
        "source": source,
        "quality": quality,
        "days": {},
        "lang_counts": {},
        "appear_days": {},
        "best_gain": {},
    }


def build_official_trends(days: dict[str, dict]) -> dict:
    dates = sorted(days)
    if not dates:
        return {"daily": [], "windows": {}}

    daily = []
    previous_day = None
    previous_ranks: dict[str, int] = {}
    for day in dates:
        current_ranks = {
            item["n"]: rank
            for rank, item in enumerate(days[day]["items"], start=1)
        }
        comparison = None
        if previous_day is not None:
            new_repos = sorted(current_ranks.keys() - previous_ranks.keys())
            repeated = current_ranks.keys() & previous_ranks.keys()
            rank_changes = sorted(
                (
                    {
                        "n": name,
                        "from": previous_ranks[name],
                        "to": current_ranks[name],
                        "change": previous_ranks[name] - current_ranks[name],
                    }
                    for name in repeated
                    if previous_ranks[name] != current_ranks[name]
                ),
                key=lambda row: (abs(row["change"]), row["n"]),
                reverse=True,
            )
            comparison = {
                "previous_date": previous_day,
                "gap_days": (date.fromisoformat(day) - date.fromisoformat(previous_day)).days,
                "new_repos": new_repos,
                "repeated_count": len(repeated),
                "rank_changes": rank_changes,
            }
        daily.append({"date": day, "count": days[day]["n"], "comparison": comparison})
        previous_day = day
        previous_ranks = current_ranks

    end_day = date.fromisoformat(dates[-1])
    windows = {}
    for window_days in (7, 30):
        start_day = end_day - timedelta(days=window_days - 1)
        rows = [
            row for row in daily
            if start_day <= date.fromisoformat(row["date"]) <= end_day
        ]
        comparable = [row["comparison"] for row in rows if row["comparison"] is not None]
        windows[f"{window_days}d"] = {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "expected_days": window_days,
            "snapshots": len(rows),
            "coverage": len(rows) / window_days,
            "comparable_snapshots": len(comparable),
            "new_entry_count": sum(len(item["new_repos"]) for item in comparable),
            "repeated_entry_count": sum(item["repeated_count"] for item in comparable),
            "rank_change_count": sum(len(item["rank_changes"]) for item in comparable),
        }
    return {"daily": daily, "windows": windows}


def official_candidates(days: dict[str, dict], window_days: int = 30) -> set[str]:
    if not days:
        return set()
    end_day = date.fromisoformat(max(days))
    cutoff = end_day - timedelta(days=window_days - 1)
    return {
        item["n"]
        for day, record in days.items()
        if cutoff <= date.fromisoformat(day) <= end_day
        for item in record["items"]
    }


def _star_change(observations: list[dict], window_days: int) -> dict:
    valid = [row for row in observations if row["status"] == "ok"]
    if not valid:
        return {"status": "insufficient"}
    end = valid[-1]
    end_day = date.fromisoformat(end["date"])
    min_interval, max_interval = (
        (6, 8) if window_days == 7 else (27, 33)
    )
    baselines = []
    for row in valid[:-1]:
        interval = (end_day - date.fromisoformat(row["date"])).days
        if min_interval <= interval <= max_interval:
            baselines.append((abs(interval - window_days), -interval, row, interval))
    if not baselines:
        return {
            "status": "insufficient",
            "end_date": end["date"],
            "valid_observations": len(valid),
        }
    _, _, baseline, interval = min(baselines, key=lambda entry: (entry[0], entry[1]))
    return {
        "status": "ok",
        "baseline_date": baseline["date"],
        "end_date": end["date"],
        "interval_days": interval,
        "delta": end["stars"] - baseline["stars"],
        "observed_days": sum(
            baseline["date"] <= row["date"] <= end["date"] for row in valid
        ),
        "expected_days": interval + 1,
    }


def load_star_tracking(stars_dir: str, active_repos: set[str]) -> dict:
    snapshots: dict[str, dict[str, dict]] = {}
    allowed_statuses = {
        "ok", "not_found", "forbidden", "rate_limited", "network_error",
        "invalid_response", "not_attempted",
    }
    for path in sorted(glob.glob(os.path.join(stars_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                snapshot = json.load(f)
            if not isinstance(snapshot, dict):
                raise ValueError("快照顶层不是对象")
            if (
                snapshot.get("schema_version") != 1
                or snapshot.get("source") != "github-repos-api"
                or snapshot.get("candidate_policy") != "official-trending-last-seen-30d"
            ):
                raise ValueError("schema_version、source 或 candidate_policy 无效")
            snapshot_day = date.fromisoformat(os.path.basename(path)[:10]).isoformat()
            if snapshot.get("date") != snapshot_day:
                raise ValueError("date 与文件名不一致")
            repos = snapshot.get("repos")
            if not isinstance(repos, list):
                raise ValueError("repos 不是列表")
            candidate_count = snapshot.get("candidate_count")
            if (
                isinstance(candidate_count, bool)
                or not isinstance(candidate_count, int)
                or candidate_count != len(repos)
            ):
                raise ValueError("candidate_count 与 repos 数量不一致")
            counts = {}
            for key in ("attempted", "http_requests", "succeeded", "failed", "not_attempted"):
                value = snapshot.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{key} 计数无效")
                counts[key] = value
            rows = {}
            status_counts = {status: 0 for status in allowed_statuses}
            for repo in repos:
                if not isinstance(repo, dict):
                    raise ValueError("仓库观测不是对象")
                name, status, stars = repo.get("name"), repo.get("status"), repo.get("stars")
                if not isinstance(name, str) or not name or name in rows:
                    raise ValueError("仓库名无效或重复")
                if status not in allowed_statuses:
                    raise ValueError(f"{name} 的状态无效")
                status_counts[status] += 1
                last_seen = repo.get("last_seen")
                if not isinstance(last_seen, str) or date.fromisoformat(last_seen).isoformat() != last_seen:
                    raise ValueError(f"{name} 的 last_seen 无效")
                if status == "ok":
                    if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
                        raise ValueError(f"{name} 的 Star 数无效")
                elif stars is not None:
                    raise ValueError(f"{name} 的失败观测 Star 数必须为 null")
                rows[name] = {"date": snapshot_day, "stars": stars, "status": status}
            if candidate_count != counts["attempted"] + counts["not_attempted"]:
                raise ValueError("候选数计数关系无效")
            if counts["attempted"] != counts["succeeded"] + counts["failed"]:
                raise ValueError("尝试数计数关系无效")
            if counts["http_requests"] < counts["attempted"]:
                raise ValueError("HTTP 请求数小于已尝试仓库数")
            if (
                status_counts["ok"] != counts["succeeded"]
                or status_counts["not_attempted"] != counts["not_attempted"]
                or len(rows) - status_counts["ok"] - status_counts["not_attempted"] != counts["failed"]
            ):
                raise ValueError("仓库状态与汇总计数不一致")
            snapshots[snapshot_day] = rows
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"[WARN] 跳过无效 Star 快照 {os.path.basename(path)}: {exc}", file=sys.stderr)

    all_days = sorted(snapshots)
    repo_rows = []
    for name in sorted(active_repos):
        observations = [
            snapshots[day][name]
            for day in all_days
            if name in snapshots[day]
        ]
        if not observations:
            repo_rows.append(
                {
                    "name": name,
                    "observations": [],
                    "change_7d": {"status": "insufficient"},
                    "change_30d": {"status": "insufficient"},
                }
            )
            continue
        repo_rows.append(
            {
                "name": name,
                "observations": observations,
                "change_7d": _star_change(observations, 7),
                "change_30d": _star_change(observations, 30),
            }
        )

    return {
        "source": "github-repos-api",
        "candidate_policy": "official-trending-last-seen-30d",
        "first": all_days[0] if all_days else None,
        "last": all_days[-1] if all_days else None,
        "snapshot_count": len(all_days),
        "repos": repo_rows,
    }


def display_path(path: str) -> str:
    """相对 ROOT 展示路径；跨盘符等无法相对化的场景回退为原路径。"""
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成看板聚合数据 dashboard/summary.json")
    parser.add_argument("--data-dir", default=DATA_DIR, help="快照目录（默认 data/）")
    parser.add_argument("--stars-dir", help="Star 观测目录（默认 <data-dir>/repo-stars/）")
    parser.add_argument("--out", default=OUT_PATH, help="输出文件（默认 dashboard/summary.json）")
    parser.add_argument("--top-items", type=int, default=12, help="每天保留的条目数（默认 12）")
    parser.add_argument("--appear-limit", type=int, default=30, help="累计热榜条数（默认 30）")
    parser.add_argument("--desc-limit", type=int, default=90, help="条目摘要长度（默认 90）")
    args = parser.parse_args()

    cohorts: dict[str, dict] = {}

    paths = sorted(glob.glob(os.path.join(args.data_dir, "*.json")))
    if not paths:
        raise SystemExit(f"[ERROR] {args.data_dir} 下没有快照文件")

    for path in paths:
        record = load_day(path)
        if record is None:
            continue
        day = os.path.basename(path)[:10]
        key = cohort_key(record["src"], record["q"])
        cohort = cohorts.setdefault(key, new_cohort(record["src"], record["q"]))
        for it in record["items"]:
            if lang := it["l"]:
                cohort["lang_counts"][lang] = cohort["lang_counts"].get(lang, 0) + 1
            name = it["n"]
            cohort["appear_days"][name] = cohort["appear_days"].get(name, 0) + 1
            if it["p"] is not None:
                prev = cohort["best_gain"].get(name)
                if prev is None or it["p"] > prev[0]:
                    cohort["best_gain"][name] = (it["p"], day)

        if record["src"] != "official":
            record["items"] = record["items"][: args.top_items]
        record["shown"] = len(record["items"])
        cohort["days"][day] = record

    if not cohorts:
        raise SystemExit("[ERROR] 没有有效快照")

    summary_cohorts = {}
    for key, cohort in cohorts.items():
        days = cohort["days"]
        sorted_dates = sorted(days)
        appear = sorted(cohort["appear_days"].items(), key=lambda kv: (-kv[1], kv[0]))[: args.appear_limit]
        appear_rows = [
            {
                "n": name,
                "days": count,
                "best": cohort["best_gain"].get(name, (None, ""))[0],
                "best_date": cohort["best_gain"].get(name, (None, ""))[1],
            }
            for name, count in appear
        ]
        langs = sorted(cohort["lang_counts"].items(), key=lambda kv: (-kv[1], kv[0]))
        max_day = max(
            (d for d in sorted_dates if days[d]["top"] is not None),
            key=lambda d: days[d]["top"],
            default=None,
        )
        summary_cohorts[key] = {
            "source": cohort["source"],
            "quality": cohort["quality"],
            "first": sorted_dates[0],
            "last": sorted_dates[-1],
            "count": len(sorted_dates),
            "langs": {"rows": [[k, v] for k, v in langs[:20]], "total": len(langs)},
            "appear": appear_rows,
            "peak": {"date": max_day, "top": days[max_day]["top"]} if max_day else None,
            "days": days,
        }

    official = summary_cohorts.get("official")
    official_data = cohorts.get("official")
    summary = {
        # 不含生成时间戳：内容只随快照数据变化，保证重复运行字节级一致（幂等）
        "cohorts": summary_cohorts,
        "official_trends": (
            build_official_trends(official["days"]) if official else {"daily": [], "windows": {}}
        ),
        "star_tracking": load_star_tracking(
            args.stars_dir or os.path.join(args.data_dir, "repo-stars"),
            official_candidates(official_data["days"]) if official_data else set(),
        ),
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    payload = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            if f.read() == payload:
                print(f"[OK] {display_path(args.out)} 无变化")
                return 0
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(payload)
    all_snapshot_dates = [
        day for cohort in summary_cohorts.values() for day in cohort["days"]
    ]
    print(
        f"[OK] {display_path(args.out)}: {len(all_snapshot_dates)} 份快照 "
        f"({min(all_snapshot_dates)} ~ {max(all_snapshot_dates)}), "
        f"{os.path.getsize(args.out) / 1024:.0f} KB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
