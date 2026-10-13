#!/usr/bin/env python3
"""Summarize Star tracking coverage and API usage for an operational review."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data" / "repo-stars"


def load_window(data_dir: Path, end_day: date, window_days: int) -> dict:
    if window_days < 1:
        raise ValueError("window-days must be at least 1")
    start_day = end_day - timedelta(days=window_days - 1)
    expected_dates = [
        (start_day + timedelta(days=index)).isoformat()
        for index in range(window_days)
    ]
    snapshots = {}
    invalid = []
    for snapshot_day in expected_dates:
        path = data_dir / f"{snapshot_day}.json"
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                snapshot = json.load(f)
            if not isinstance(snapshot, dict):
                raise ValueError("快照顶层不是对象")
            if (
                snapshot.get("schema_version") != 1
                or snapshot.get("date") != snapshot_day
                or snapshot.get("source") != "github-repos-api"
                or snapshot.get("candidate_policy") != "official-trending-last-seen-30d"
            ):
                raise ValueError("schema_version、date、source 或 candidate_policy 无效")

            counts = {}
            for key in (
                "candidate_count",
                "attempted",
                "http_requests",
                "succeeded",
                "failed",
                "not_attempted",
            ):
                value = snapshot.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{key} 计数无效")
                counts[key] = value
            if counts["candidate_count"] != counts["attempted"] + counts["not_attempted"]:
                raise ValueError("candidate_count 计数关系无效")
            if counts["attempted"] != counts["succeeded"] + counts["failed"]:
                raise ValueError("attempted 计数关系无效")
            if counts["http_requests"] < counts["attempted"]:
                raise ValueError("http_requests 小于 attempted")

            repos = snapshot.get("repos")
            if not isinstance(repos, list) or len(repos) != counts["candidate_count"]:
                raise ValueError("repos 数量与候选数不一致")
            names = set()
            statuses = {"ok": 0, "not_attempted": 0, "failed": 0}
            failed_statuses = {
                "not_found", "forbidden", "rate_limited", "network_error", "invalid_response"
            }
            for repo in repos:
                if not isinstance(repo, dict):
                    raise ValueError("仓库记录不是对象")
                name = repo.get("name")
                status = repo.get("status")
                if not isinstance(name, str) or not name or name in names:
                    raise ValueError("仓库名无效或重复")
                names.add(name)
                if status == "ok":
                    stars = repo.get("stars")
                    if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
                        raise ValueError(f"{name} 的 Star 数无效")
                    statuses["ok"] += 1
                elif status == "not_attempted":
                    if repo.get("stars") is not None:
                        raise ValueError(f"{name} 的未请求 Star 数必须为 null")
                    statuses["not_attempted"] += 1
                elif status in failed_statuses:
                    if repo.get("stars") is not None:
                        raise ValueError(f"{name} 的失败 Star 数必须为 null")
                    statuses["failed"] += 1
                else:
                    raise ValueError(f"{name} 的状态无效")
            if (
                statuses["ok"] != counts["succeeded"]
                or statuses["not_attempted"] != counts["not_attempted"]
                or statuses["failed"] != counts["failed"]
            ):
                raise ValueError("仓库状态与汇总计数不一致")

            duration = snapshot.get("duration_seconds")
            if duration is not None and (
                isinstance(duration, bool) or not isinstance(duration, int) or duration < 0
            ):
                raise ValueError("duration_seconds 无效")
            remaining = snapshot.get("rate_limit_remaining")
            if remaining is not None and (
                isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0
            ):
                raise ValueError("rate_limit_remaining 无效")
            snapshots[snapshot_day] = snapshot
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            invalid.append({"date": snapshot_day, "reason": str(exc)})

    totals = {
        key: sum(snapshot[key] for snapshot in snapshots.values())
        for key in (
            "candidate_count",
            "attempted",
            "http_requests",
            "succeeded",
            "failed",
            "not_attempted",
        )
    }
    attempted = totals["attempted"]
    candidates = totals["candidate_count"]
    durations = [
        snapshot["duration_seconds"]
        for snapshot in snapshots.values()
        if "duration_seconds" in snapshot
    ]
    limits = [
        snapshot["rate_limit_remaining"]
        for snapshot in snapshots.values()
        if isinstance(snapshot.get("rate_limit_remaining"), int)
        and not isinstance(snapshot.get("rate_limit_remaining"), bool)
    ]
    incomplete_dates = [
        day for day, snapshot in snapshots.items()
        if snapshot["failed"] or snapshot["not_attempted"]
    ]
    valid_dates = sorted(snapshots)
    invalid_dates = {row["date"] for row in invalid}
    return {
        "source": "github-repos-api",
        "window": {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "expected_days": window_days,
            "snapshot_days": len(valid_dates),
            "valid_dates": valid_dates,
            "missing_dates": [
                day for day in expected_dates if day not in snapshots and day not in invalid_dates
            ],
            "invalid_snapshots": invalid,
            "incomplete_dates": incomplete_dates,
            "complete": len(snapshots) == window_days and not invalid,
        },
        "totals": {
            **totals,
            "success_rate": totals["succeeded"] / attempted if attempted else None,
            "candidate_success_rate": totals["succeeded"] / candidates if candidates else None,
        },
        "runtime_seconds": {
            "observed_days": len(durations),
            "total": sum(durations) if durations else None,
            "average": sum(durations) / len(durations) if durations else None,
            "maximum": max(durations) if durations else None,
        },
        "rate_limit_remaining": {
            "observed_days": len(limits),
            "minimum": min(limits) if limits else None,
            "latest": snapshots[valid_dates[-1]].get("rate_limit_remaining") if valid_dates else None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="输出 Star 追踪近 N 日覆盖率、成功率、配额和耗时复盘摘要"
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    beijing = timezone(timedelta(hours=8))
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=datetime.now(beijing).date(),
        help="复盘窗口结束日期（默认北京时间今天）",
    )
    parser.add_argument("--window-days", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        report = load_window(args.data_dir, args.end_date, args.window_days)
    except ValueError as exc:
        parser.error(str(exc))
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
