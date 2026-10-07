# GitHubNews — GitHub 热点项目追踪

由 GitHub Actions 每日定时抓取 GitHub 热点项目，自动提交榜单报告（`reports/`）与数据快照（`data/`，含 README 摘录）；本地 agent 基于快照生成问题与场景分析，产出 `analysis/` 下的分析报告（人工审核后入库）。

| 报告 | 生成频率 | 口径 |
|------|----------|------|
| 日报 | 每天 08:00（北京时间） | [GitHub Trending](https://github.com/trending) 官方口径（daily） |
| 月报 | 每月 1 日 | GitHub Trending 官方口径（monthly） |
| 年报 | 每年 1 月 1 日 | Search API：近一年创建且 Star 最高的新项目 Top 25 |
| 分析 | 持续更新 | 基于 `data/` 快照的问题与场景分析（人工审核入库） |

<!-- reports:index:start -->
## 最新报告

- **日报**：[2026-10-06](reports/daily/2026-10-06.md)
- **月报**：暂无
- **年报**：[2026](reports/yearly/2026.md)

## 历史报告

### 日报（最新在前）

- [2026-10-06](reports/daily/2026-10-06.md)

### 月报（最新在前）

- 暂无

### 年报（最新在前）

- [2026](reports/yearly/2026.md)

<!-- reports:index:end -->

<!-- analysis:index:start -->
## 项目分析

- [2026-10-06-skills](analysis/2026-10-06-skills.md)
- [2026-10-06-rea](analysis/2026-10-06-rea.md)
- [2026-10-06-e2e](analysis/2026-10-06-e2e.md)
- [2026-10-06-DeepGEMM](analysis/2026-10-06-DeepGEMM.md)
- [2026-10-06-claude-mem](analysis/2026-10-06-claude-mem.md)
<!-- analysis:index:end -->

## 开发

以下内容位于标记区块之外，脚本运行时原样保留，可自由编辑。

```bash
# 本地运行（需已安装 uv，依赖由脚本头部的 PEP 723 元数据自动解析）
uv run scripts/fetch_trending.py                # 按当天日期自动判断生成日报/月报/年报
uv run scripts/fetch_trending.py --only daily   # 只生成指定周期（daily/monthly/yearly 可多次使用）

# 传统方式
pip install -r requirements.txt
python scripts/fetch_trending.py --only daily

# 运行测试
uv run --with pytest --with requests --with beautifulsoup4 pytest -q
```

本地运行提示：README 摘录需要访问 `api.github.com`，未认证限额为 60 次/小时/IP，建议设置 `GITHUB_TOKEN` 环境变量（每日报告约需 25 次，重试上限 50 次）。

## 目录结构

```
├── .github/workflows/   # CI：每日报告（trending.yml）与测试（tests.yml）
├── analysis/            # 人工审核入库的项目分析（见 analysis/README.md）
├── data/                # 每日数据快照 JSON（含 README 摘录，供分析消费）
├── reports/             # 自动生成的榜单报告（daily / monthly / yearly）
├── scripts/             # fetch_trending.py 主脚本
└── tests/               # 回归测试
```
