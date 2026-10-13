#!/usr/bin/env python3
"""Record daily GitHub star-count observations for recent official trending repos."""
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests==2.34.2",
#     "beautifulsoup4==4.15.0",
# ]
# ///
# Keep dependency versions in sync with requirements.txt.

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from fetch_trending import UA, get_with_retry, now_bj

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = DATA_DIR / "repo-stars"
WINDOW_DAYS = 30
MAX_REQUESTS = 400
MAX_RUNTIME_SECONDS = 480
REQUEST_TIMEOUT_SECONDS = 10
RETRY_ATTEMPTS = 3
MAX_REQUEST_WALL_SECONDS = 60
REPO_STATUSES = {
    "ok",
    "not_found",
    "forbidden",
    "rate_limited",
    "network_error",
    "invalid_response",
    "not_attempted",
}


def load_candidates(data_dir: Path, end_day: date, window_days: int = WINDOW_DAYS) -> dict[str, str]:
    """Return repo names mapped to their latest official-trending date."""
    first_day = end_day - timedelta(days=window_days - 1)
    latest_seen: dict[str, str] = {}

    for path in sorted(data_dir.glob("*.json")):
        try:
            snapshot_day = date.fromisoformat(path.stem)
        except ValueError:
            print(f"[WARN] 忽略文件名不是日期的快照: {path.name}", file=sys.stderr)
            continue
        if snapshot_day < first_day or snapshot_day > end_day:
            continue

        try:
            with path.open(encoding="utf-8") as f:
                snapshot = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取候选快照 {path.name}: {exc}") from exc
        if not isinstance(snapshot, dict):
            raise RuntimeError(f"候选快照格式错误: {path.name}")

        declared_day = snapshot.get("date")
        if declared_day != snapshot_day.isoformat():
            raise RuntimeError(
                f"快照日期与文件名不一致: {path.name} (date={declared_day!r})"
            )
        source = snapshot.get("source") or "official"
        if source != "official":
            continue

        items = snapshot.get("daily")
        if not isinstance(items, list) or not items:
            raise RuntimeError(f"官方快照缺少 daily 列表: {path.name}")
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError(f"官方快照包含无效仓库条目: {path.name}")
            name = item.get("name")
            if not isinstance(name, str):
                raise RuntimeError(f"官方快照仓库名无效: {path.name}")
            name = name.strip()
            if name.count("/") != 1 or any(not part for part in name.split("/")):
                raise RuntimeError(f"官方快照仓库名格式无效: {path.name} ({name!r})")
            latest_seen[name] = snapshot_day.isoformat()

    return latest_seen


def _rate_remaining(headers: object) -> int | None:
    try:
        value = headers.get("X-RateLimit-Remaining")
        return int(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _is_rate_limited(response: requests.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if _rate_remaining(response.headers) == 0 or response.headers.get("Retry-After"):
        return True
    try:
        payload = response.json()
        message = payload.get("message", "") if isinstance(payload, dict) else ""
    except (ValueError, AttributeError):
        message = ""
    return isinstance(message, str) and "rate limit" in message.lower()


def _status_for_response(response: requests.Response) -> tuple[str, int | None, str | None]:
    remaining = _rate_remaining(response.headers)
    if response.status_code == 200:
        try:
            payload = response.json()
        except (ValueError, requests.JSONDecodeError):
            return "invalid_response", None, "API 返回的 JSON 无法解析"
        if not isinstance(payload, dict):
            return "invalid_response", None, "API 返回结构不是对象"
        stars = payload.get("stargazers_count")
        if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
            return "invalid_response", None, "API 未返回有效的 stargazers_count"
        return "ok", stars, None
    if _is_rate_limited(response):
        return "rate_limited", None, f"HTTP {response.status_code}: API 限流"
    if response.status_code == 404:
        return "not_found", None, "HTTP 404: 仓库不存在或不可访问"
    if response.status_code in (401, 403):
        return "forbidden", None, f"HTTP {response.status_code}: API 拒绝访问"
    return "network_error", None, f"HTTP {response.status_code}: API 请求失败"


def _request_repo(
    session: requests.Session, name: str, token: str, attempts: int
) -> tuple[str, int | None, str | None, int | None]:
    response = get_with_retry(
        session,
        f"https://api.github.com/repos/{name}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
        attempts=attempts,
    )
    status, stars, error = _status_for_response(response)
    return status, stars, error, _rate_remaining(response.headers)


class CountingSession:
    def __init__(self, session: requests.Session):
        self.session = session
        self.headers = session.headers
        self.request_count = 0

    def get(self, *args, **kwargs):
        self.request_count += 1
        return self.session.get(*args, **kwargs)


def collect_snapshot(
    candidates: dict[str, str],
    token: str,
    target_day: date,
    *,
    max_requests: int = MAX_REQUESTS,
    max_runtime_seconds: int = MAX_RUNTIME_SECONDS,
    session: requests.Session | None = None,
) -> tuple[dict, int]:
    if max_requests < 0:
        raise ValueError("--max-requests must be non-negative")
    if max_runtime_seconds < 0:
        raise ValueError("--max-runtime-seconds must be non-negative")

    client = session or requests.Session()
    client.headers.update({"User-Agent": UA})
    counted = CountingSession(client)
    ordered = sorted(
        candidates,
        key=lambda name: (-date.fromisoformat(candidates[name]).toordinal(), name),
    )
    started = time.monotonic()
    repos = []
    attempted = 0
    succeeded = 0
    failed = 0
    rate_remaining = None
    stop_reason = None

    for index, name in enumerate(ordered):
        requests_left = max_requests - counted.request_count
        if requests_left <= 0:
            stop_reason = f"请求预算已达上限 ({max_requests})"
            break
        elapsed = time.monotonic() - started
        if max_runtime_seconds - elapsed < MAX_REQUEST_WALL_SECONDS:
            stop_reason = f"运行时间预算已达上限 ({max_runtime_seconds}s)"
            break

        attempted += 1
        repo_rate_remaining = None
        try:
            status, stars, error, rate_remaining = _request_repo(
                counted, name, token, min(RETRY_ATTEMPTS, requests_left)
            )
            repo_rate_remaining = rate_remaining
        except requests.RequestException as exc:
            status, stars = "network_error", None
            error = f"网络/API 请求失败: {type(exc).__name__}"
        except (ValueError, TypeError) as exc:
            status, stars = "invalid_response", None
            error = f"API 返回无效数据: {type(exc).__name__}"

        if status == "ok":
            succeeded += 1
        else:
            failed += 1
        repos.append(
            {
                "name": name,
                "stars": stars,
                "status": status,
                "last_seen": candidates[name],
                **({"error": error} if error else {}),
            }
        )
        rate_remaining = repo_rate_remaining

        if status == "rate_limited":
            stop_reason = error
            break
        if status == "forbidden":
            stop_reason = "GitHub API 拒绝访问，停止后续请求"
            break
        if rate_remaining == 0 and index + 1 < len(ordered):
            stop_reason = "GitHub API 主限额已耗尽"
            break

    completed_names = {repo["name"] for repo in repos}
    not_attempted = [
        {
            "name": name,
            "stars": None,
            "status": "not_attempted",
            "last_seen": candidates[name],
        }
        for name in ordered
        if name not in completed_names
    ]
    repos.extend(not_attempted)
    snapshot = {
        "schema_version": 1,
        "date": target_day.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "github-repos-api",
        "candidate_policy": "official-trending-last-seen-30d",
        "candidate_count": len(ordered),
        "attempted": attempted,
        "http_requests": counted.request_count,
        "succeeded": succeeded,
        "failed": failed,
        "not_attempted": len(not_attempted),
        "rate_limit_remaining": rate_remaining,
        "duration_seconds": int(time.monotonic() - started),
        "repos": repos,
    }
    if stop_reason:
        snapshot["stop_reason"] = stop_reason

    if snapshot["candidate_count"] != snapshot["attempted"] + snapshot["not_attempted"]:
        raise RuntimeError("候选数计数校验失败")
    if snapshot["attempted"] != snapshot["succeeded"] + snapshot["failed"]:
        raise RuntimeError("请求数计数校验失败")
    return snapshot, int(time.monotonic() - started)


def validate_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict):
        raise ValueError("Star 快照必须是 JSON 对象")
    if snapshot.get("schema_version") != 1:
        raise ValueError("Star 快照 schema_version 必须为 1")
    try:
        parsed_day = date.fromisoformat(snapshot["date"])
        fetched_at = datetime.fromisoformat(snapshot["fetched_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Star 快照日期或 fetched_at 无效") from exc
    if parsed_day.isoformat() != snapshot["date"] or fetched_at.utcoffset() is None:
        raise ValueError("Star 快照日期必须为 ISO 格式，fetched_at 必须包含时区")
    if snapshot.get("source") != "github-repos-api":
        raise ValueError("Star 快照 source 无效")
    if snapshot.get("candidate_policy") != "official-trending-last-seen-30d":
        raise ValueError("Star 快照 candidate_policy 无效")
    for key in ("candidate_count", "attempted", "succeeded", "failed", "not_attempted"):
        value = snapshot.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Star 快照计数无效: {key}")
    http_requests = snapshot.get("http_requests")
    if isinstance(http_requests, bool) or not isinstance(http_requests, int) or http_requests < 0:
        raise ValueError("Star 快照 http_requests 无效")
    duration_seconds = snapshot.get("duration_seconds")
    if duration_seconds is not None and (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds < 0
    ):
        raise ValueError("Star 快照 duration_seconds 无效")
    repos = snapshot.get("repos")
    if not isinstance(repos, list):
        raise ValueError("Star 快照 repos 必须是列表")
    if snapshot["candidate_count"] != len(repos):
        raise ValueError("候选数与仓库记录数不一致")

    names = set()
    status_counts = {status: 0 for status in REPO_STATUSES}
    for repo in repos:
        if not isinstance(repo, dict):
            raise ValueError("仓库观测必须是对象")
        name = repo.get("name")
        status = repo.get("status")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("仓库名为空或重复")
        names.add(name)
        if not isinstance(status, str) or status not in REPO_STATUSES:
            raise ValueError(f"未知仓库观测状态: {status}")
        try:
            date.fromisoformat(repo["last_seen"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{name} 的 last_seen 无效") from exc
        status_counts[status] += 1
        stars = repo.get("stars")
        if status == "ok":
            if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
                raise ValueError(f"{name} 的 Star 数无效")
        elif stars is not None:
            raise ValueError(f"{name} 的失败观测必须使用 null Star 数")

    if snapshot["attempted"] + snapshot["not_attempted"] != snapshot["candidate_count"]:
        raise ValueError("候选数计数校验失败")
    if snapshot["attempted"] != snapshot["succeeded"] + snapshot["failed"]:
        raise ValueError("请求数计数校验失败")
    if http_requests < snapshot["attempted"]:
        raise ValueError("HTTP 请求数不能少于已尝试仓库数")
    if (
        status_counts["ok"] != snapshot["succeeded"]
        or status_counts["not_attempted"] != snapshot["not_attempted"]
        or len(repos) - status_counts["ok"] - status_counts["not_attempted"] != snapshot["failed"]
    ):
        raise ValueError("仓库状态与计数不一致")


def write_snapshot(snapshot: dict, out_dir: Path) -> Path:
    validate_snapshot(snapshot)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{snapshot['date']}.json"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=out_dir,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_path = Path(f.name)
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.write("\n")
        with temp_path.open(encoding="utf-8") as f:
            json.load(f)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="追踪近期官方 Trending 仓库的每日 Star 总数")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="官方快照目录（默认 data/）")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR, help="输出目录（默认 data/repo-stars/）")
    parser.add_argument("--date", type=date.fromisoformat, help="快照日期 YYYY-MM-DD（默认北京时间今天）")
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS, help="候选窗口日历天数（默认 30）")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=MAX_REQUESTS,
        help="最大 HTTP 请求数（包含重试，默认 400）",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=MAX_RUNTIME_SECONDS,
        help="最大查询时间秒数（默认 480）",
    )
    args = parser.parse_args(argv)
    if args.window_days < 1:
        parser.error("--window-days must be at least 1")

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("[ERROR] 请通过 GITHUB_TOKEN 环境变量提供 GitHub API token。", file=sys.stderr)
        return 2

    target_day = args.date or now_bj().date()
    try:
        candidates = load_candidates(args.data_dir, target_day, args.window_days)
        snapshot, duration = collect_snapshot(
            candidates,
            token,
            target_day,
            max_requests=args.max_requests,
            max_runtime_seconds=args.max_runtime_seconds,
        )
        path = write_snapshot(snapshot, args.out_dir)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] Star 快照采集失败: {exc}", file=sys.stderr)
        return 1

    print(
        f"[OK] Star 快照 -> {path} "
        f"(candidates={snapshot['candidate_count']}, attempted={snapshot['attempted']}, "
        f"http_requests={snapshot['http_requests']}, "
        f"succeeded={snapshot['succeeded']}, failed={snapshot['failed']}, "
        f"not_attempted={snapshot['not_attempted']}, "
        f"rate_limit_remaining={snapshot['rate_limit_remaining']}, duration={duration}s)"
    )
    if snapshot["failed"] or snapshot["not_attempted"]:
        print(f"[WARN] Star 观测不完整: {snapshot.get('stop_reason', '存在失败仓库')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
