# GitHubNews — GitHub 热点项目追踪

由 GitHub Actions 每日定时抓取 GitHub 热点项目，自动提交榜单报告（`reports/`）与数据快照（`data/`，含 README 摘录）；本地 agent 基于快照生成问题与场景分析，产出 `analysis/` 下的分析报告（人工审核后入库）。

| 报告 | 生成频率 | 口径 |
|------|----------|------|
| 日报 | 每天 06:30（北京时间） | [GitHub Trending](https://github.com/trending) 官方口径（daily，2026-10 起官方页面每日仅 12 条） |
| 月报 | 每月 1 日 | GitHub Trending 官方口径（monthly） |
| 年报 | 每年 1 月 1 日 | Search API：近一年创建且 Star 最高的新项目 Top 25 |
| 分析 | 持续更新 | 基于 `data/` 快照的问题与场景分析（人工审核入库） |
| 看板 | 随日报更新 | 静态可视化页面，汇总浏览全部快照（[dashboard/](dashboard/)） |
| 历史回填 | 一次性 | GH Archive WatchEvent：每日新增 Star Top 100（2025-01-01 ~ 2026-03-31，口径与质量分级见 [analysis/README.md](analysis/README.md)） |

<!-- reports:index:start -->
## 最新报告

- **日报**：[2026-10-25](reports/daily/2026-10-25.md)
- **月报**：暂无
- **年报**：[2026](reports/yearly/2026.md)

## 历史报告

### 日报（最新在前）

- [2026-10-25](reports/daily/2026-10-25.md)
- [2026-10-24](reports/daily/2026-10-24.md)
- [2026-10-23](reports/daily/2026-10-23.md)
- [2026-10-22](reports/daily/2026-10-22.md)
- [2026-10-21](reports/daily/2026-10-21.md)
- [2026-10-20](reports/daily/2026-10-20.md)
- [2026-10-19](reports/daily/2026-10-19.md)
- [2026-10-18](reports/daily/2026-10-18.md)
- [2026-10-17](reports/daily/2026-10-17.md)
- [2026-10-16](reports/daily/2026-10-16.md)
- [2026-10-15](reports/daily/2026-10-15.md)
- [2026-10-14](reports/daily/2026-10-14.md)
- [2026-10-13](reports/daily/2026-10-13.md)
- [2026-10-12](reports/daily/2026-10-12.md)
- [2026-10-11](reports/daily/2026-10-11.md)
- [2026-10-10](reports/daily/2026-10-10.md)
- [2026-10-09](reports/daily/2026-10-09.md)
- [2026-10-08](reports/daily/2026-10-08.md)
- [2026-10-07](reports/daily/2026-10-07.md)
- [2026-10-06](reports/daily/2026-10-06.md)

### 月报（最新在前）

- 暂无

### 年报（最新在前）

- [2026](reports/yearly/2026.md)

<!-- reports:index:end -->

<!-- analysis:index:start -->
## 项目分析

- [2026-10-10-analysis](analysis/2026-10-10-analysis.md)
- [2026-10-07-text-to-cad](analysis/2026-10-07-text-to-cad.md)
- [2026-10-07-openGym](analysis/2026-10-07-openGym.md)
- [2026-10-07-impeccable](analysis/2026-10-07-impeccable.md)
- [2026-10-07-AnyPS5](analysis/2026-10-07-AnyPS5.md)
- [2026-10-06-skills](analysis/2026-10-06-skills.md)
- [2026-10-06-rea](analysis/2026-10-06-rea.md)
- [2026-10-06-e2e](analysis/2026-10-06-e2e.md)
- [2026-10-06-claude-mem](analysis/2026-10-06-claude-mem.md)
- [2026-10-06-analysis](analysis/2026-10-06-analysis.md)
- [2026-10-06-DeepGEMM](analysis/2026-10-06-DeepGEMM.md)
<!-- analysis:index:end -->

## 开发

以下内容位于标记区块之外，脚本运行时原样保留，可自由编辑。

```bash
# 本地运行（需已安装 uv，依赖由脚本头部的 PEP 723 元数据自动解析）
uv run scripts/fetch_trending.py                # 按当天日期自动判断生成日报/月报/年报
uv run scripts/fetch_trending.py --only daily   # 只生成指定周期（daily/monthly/yearly 可多次使用）
uv run scripts/track_stars.py                   # 追踪最近 30 天官方榜单项目的总 Star（需 GITHUB_TOKEN）
uv run scripts/review_star_tracking.py --window-days 30  # 按北京时间今天结束，复核覆盖/成功率/配额/耗时

# 历史回填：GH Archive WatchEvent 每日新增 Star Top 100（幂等，已有日期跳过）
uv run scripts/backfill_trending.py             # 默认 2025-01-01 → 2026-03-31（事件流可靠性截止）
uv run scripts/backfill_trending.py --start 2025-06-01 --end 2025-06-30

# 传统方式
pip install -r requirements.txt
python scripts/fetch_trending.py --only daily

# 看板：重新生成榜单与 Star 趋势聚合并本地预览（打开 http://127.0.0.1:8000/dashboard/）
uv run python scripts/build_dashboard.py
uv run python -m http.server 8000

# 运行测试
uv run --with pytest --with requests --with beautifulsoup4 pytest -q
```

本地运行提示：README 摘录需要访问 `api.github.com`，未认证限额为 60 次/小时/IP，建议设置 `GITHUB_TOKEN` 环境变量（每日报告约需 25 次，重试上限 50 次）。
Star 追踪使用 GitHub Repositories API，需要设置 `GITHUB_TOKEN`；每日观测独立保存到 `data/repo-stars/`，只追踪最近 30 个日历日内出现在官方 Trending（日榜）的仓库，历史回填项目不会纳入。默认最多发起 400 次 HTTP 请求（重试计入上限）、最多运行 480 秒；若达到预算或 API 限流，会保存已观测与未执行状态并以非零状态告警，不影响工作流继续生成日报。
看板趋势只对官方榜单计算新/重复上榜和共同上榜项目排名变化；Star 增长只使用 API 成功观测，并在有效基线存在时展示 7/30 日差值、实际间隔与观测覆盖。缺少数据不补零，GH Archive 回填仍独立显示。

## 目录结构

```
├── .github/workflows/   # CI：每日报告（trending.yml）与测试（tests.yml）
├── analysis/            # 人工审核入库的项目分析（见 analysis/README.md）
├── dashboard/           # 数据看板：index.html 静态页面 + summary.json 聚合（build_dashboard.py 生成）
├── data/                # 每日榜单快照与 repo-stars/ 仓库每日总 Star 观测
├── reports/             # 自动生成的榜单报告（daily / monthly / yearly）
├── scripts/             # 日常抓取、Star 追踪与复盘、历史回填、看板聚合
└── tests/               # 回归测试
```
