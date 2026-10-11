#!/usr/bin/env python3
"""汇总 data/ 下的每日快照，生成看板所需的 dashboard/summary.json。

只依赖标准库，trending 工作流在提交报告前运行；本地可手动执行：

    uv run python scripts/build_dashboard.py

summary.json 是按天与按仓库两个粒度的压缩聚合（每天保留前 N 条），
看板页（dashboard/index.html）通过相对路径加载它；完整榜单仍以
data/YYYY-MM-DD.json 为准，页面里给出链接。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

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

    day = data.get("date") or os.path.basename(path)[:10]
    items = data.get("daily")
    if not isinstance(items, list) or not items:
        print(f"[WARN] 跳过空快照 {os.path.basename(path)}", file=sys.stderr)
        return None

    source = data.get("source") or "official"
    normalized = []
    for pos, it in enumerate(items):
        if not isinstance(it, dict) or not it.get("name"):
            continue
        normalized.append(
            {
                "n": it["name"],
                "s": to_int(it.get("stars")),
                "p": to_int(it.get("period_stars")),
                "l": (it.get("lang") or "").strip(),
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
        # 榜单顺序即排名；回填数据自带 rank，官方快照由顺序隐含
        "items": normalized,
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
    parser.add_argument("--out", default=OUT_PATH, help="输出文件（默认 dashboard/summary.json）")
    parser.add_argument("--top-items", type=int, default=12, help="每天保留的条目数（默认 12）")
    parser.add_argument("--appear-limit", type=int, default=30, help="累计热榜条数（默认 30）")
    parser.add_argument("--desc-limit", type=int, default=90, help="条目摘要长度（默认 90）")
    args = parser.parse_args()

    days = {}
    lang_counts: dict[str, int] = {}
    appear_days: dict[str, int] = {}
    best_gain: dict[str, tuple[int, str]] = {}

    paths = sorted(glob.glob(os.path.join(args.data_dir, "*.json")))
    if not paths:
        raise SystemExit(f"[ERROR] {args.data_dir} 下没有快照文件")

    for path in paths:
        record = load_day(path)
        if record is None:
            continue
        day = os.path.basename(path)[:10]

        for it in record["items"]:
            if lang := it["l"]:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
            name = it["n"]
            appear_days[name] = appear_days.get(name, 0) + 1
            if it["p"] is not None:
                prev = best_gain.get(name)
                if prev is None or it["p"] > prev[0]:
                    best_gain[name] = (it["p"], day)

        record["items"] = record["items"][: args.top_items]
        days[day] = record

    sorted_dates = sorted(days)
    appear = sorted(appear_days.items(), key=lambda kv: (-kv[1], kv[0]))[: args.appear_limit]
    appear_rows = [
        {
            "n": name,
            "days": count,
            "best": best_gain.get(name, (None, ""))[0],
            "best_date": best_gain.get(name, (None, ""))[1],
        }
        for name, count in appear
    ]
    langs = sorted(lang_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    max_day = max(
        (d for d in sorted_dates if days[d]["top"] is not None),
        key=lambda d: days[d]["top"],
        default=None,
    )

    summary = {
        # 不含生成时间戳：内容只随快照数据变化，保证重复运行字节级一致（幂等）
        "first": sorted_dates[0],
        "last": sorted_dates[-1],
        "count": len(sorted_dates),
        "langs": {"rows": [[k, v] for k, v in langs[:20]], "total": len(langs)},
        "appear": appear_rows,
        "peak": {"date": max_day, "top": days[max_day]["top"]} if max_day else None,
        "days": days,
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
    print(
        f"[OK] {display_path(args.out)}: {len(sorted_dates)} 天 "
        f"({sorted_dates[0]} ~ {sorted_dates[-1]}), "
        f"{os.path.getsize(args.out) / 1024:.0f} KB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
