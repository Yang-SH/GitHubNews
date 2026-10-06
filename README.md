# GitHubNews — GitHub 热点项目追踪

由 GitHub Actions 每日定时抓取 GitHub 热点项目，自动提交榜单报告（`reports/`）与数据快照（`data/`，含 README 摘录）；本地 agent 基于快照生成问题与场景分析，产出 `analysis/` 下的分析报告（人工审核后入库）。

| 报告 | 生成频率 | 口径 |
|------|----------|------|
| 日报 | 每天 08:00（北京时间） | [GitHub Trending](https://github.com/trending) 官方口径（daily） |
| 月报 | 每月 1 日 | GitHub Trending 官方口径（monthly） |
| 年报 | 每年 1 月 1 日 | Search API：近一年创建且 Star 最高的新项目 Top 25 |

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
