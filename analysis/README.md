# 项目分析（analysis/）

本地 agent 基于 `data/` 下的每日快照生成"问题与场景"分析草稿，经人工审核修改后提交到本目录入库，并由根 README 的"项目分析"索引自动收录（`scripts/fetch_trending.py` 每次运行时刷新索引，无需手工维护）。

## 工作流程

1. **选定快照**：从 `data/YYYY-MM-DD.json` 取当日榜单。字段说明：
   - `name` / `url`：仓库全名与链接
   - `desc`：项目简介
   - `lang`：语言（缺省为"其他"）
   - `stars`：总 Star（千分位字符串）
   - `period_stars`：周期增长（日报为今日 Star）
   - `created`：仓库创建日期（仅年报条目；年报条目由 Search API 生成，无 `period_stars`）
   - `readme`：项目 README 摘录（前 2000 字符，供理解项目用途）
   - `rank`：当日总榜排名（仅历史回填条目；官方快照的排名由条目顺序隐含）

   文件顶层除 `date` / `fetched_at` 外，历史回填文件另有 `source`（数据来源）与
   `quality`（质量分级）两个字段；官方抓取的快照没有这两个字段。
2. **agent 生成草稿**：按下方模板撰写，产出给人工，**不直接入库**。
3. **人工审核**：核对事实（Star 数、功能描述、快照日期）、删改观点、确认无误。
4. **入库**：按命名规范保存到本目录并提交；根 README 索引在脚本下次运行时自动收录。

## 每日 Star 连续观测（data/repo-stars/）

`scripts/track_stars.py` 每日读取最近 30 个日历日的官方日榜快照，并查询 GitHub
Repositories API 的仓库总 Star，输出到 `data/repo-stars/YYYY-MM-DD.json`。候选只来自
官方 Trending，不包含 GH Archive 历史回填或 Search API 年报项目。此目录与
`data/YYYY-MM-DD.json` 榜单快照分开，避免不同来源被当成同一种榜单数据。

- `stars` 是 API 返回的累计 Star 数；首次观测只建立基线，不代表当日新增。
- `status` 为 `ok` 时 `stars` 是非负整数；失败或未请求时为 `null`，不得解释为 0。状态包括 `not_found`、`forbidden`、`rate_limited`、`network_error`、`invalid_response` 和 `not_attempted`。
- `last_seen` 记录该仓库最近一次出现在官方日榜的日期。
- 文件记录候选数、至少发起一次请求的仓库数、实际 HTTP 请求数（含重试）、成功/失败数、未请求数和 API 剩余额度。
- 遇到限流或预算上限时，剩余候选标为 `not_attempted`；采集会告警且不阻断日报工作流。
- 7/30 日差值必须使用两次有效 API 总 Star 观测计算，不能使用 `period_stars` 或 GH Archive `stars` 作为基线。
- `GITHUB_TOKEN` 仅通过环境变量提供；本地可运行 `uv run scripts/track_stars.py`，缺少 token 时脚本明确报错且不写成功快照。
- 采集快照包含 `duration_seconds`，用于运行复核。试运行窗口结束后，可运行
  `uv run python scripts/review_star_tracking.py --end-date YYYY-MM-DD --window-days 30`
  汇总覆盖、缺失/无效日期、候选和请求成功率、耗时及 API 剩余额度；缺失统计不会被补零。

## 看板趋势口径

`uv run python scripts/build_dashboard.py` 会从 `data/YYYY-MM-DD.json` 和
`data/repo-stars/YYYY-MM-DD.json` 生成 `dashboard/summary.json`。官方榜单变化
只使用官方 Trending 快照，按相邻有效快照对比；名次只比较两次都在榜的仓库，
缺失日期不会当作未上榜。近 7/30 日覆盖率按日历窗口的实际官方快照数计算，
并显示有效日期数，不会把窗口缺口补成零。

Star 时间序列仅展示最近 30 个日历日内上过官方榜的候选仓库。差值端点和基线
必须是 API 成功观测，7 日基线允许相隔 6–8 天，30 日基线允许相隔 27–33 天；
页面展示实际间隔及基线至结束日期间有效观测数。无合格基线、失败请求或未请求
均不折算为零；GH Archive 回填指标不进入这两项趋势。

## 命名规范

`YYYY-MM-DD-<slug>.md`，日期取所依据快照的日期，slug 用仓库短名，如 `2026-10-06-claude-mem.md`。

## 报告模板

```markdown
# <项目名>：<一句话定位>

- **仓库**：<owner/repo 链接>
- **依据快照**：YYYY-MM-DD（当日总榜第 N 位，<语言>，总 Star <x>）
- **分析日期**：YYYY-MM-DD
- **审核状态**：已人工审核

## 是什么

<项目本身是什么，2-3 句，基于 README 摘录与官方描述。>

## 解决什么问题

<针对的痛点，与现有方案的差异。>

## 适用场景

<谁该用、什么时候用，2-4 条。>

## 潜在问题与风险

<成熟度、维护活跃度、许可证、替代方案等值得注意的点。>
```

## 历史回填数据（source=gharchive-watchevent）

GitHub Trending 页面不支持历史查询，2025-01-01 ~ 2026-03-31 的历史快照由
`scripts/backfill_trending.py` 回填：用 GH Archive 的 WatchEvent（Star 事件）
在 ClickHouse 公共数据集（play.clickhouse.com）上按 UTC 天重算"当日新增
Star Top 100"。

**与官方 Trending 的口径差异（分析时务必注意）：**

- **排序口径**为"当日新增 Star 数"；官方 Trending 由 GitHub 私有算法排序
  （综合 Star 增长、浏览量等），两者高度相关但不完全一致。
- **`stars` 是推算下界**：GitHub 公共事件流自 2025 年起渐进丢失 Star 事件
  （对照 API 真实星数标定：2025 年约捕获 60%-90%，此后持续恶化），`stars`
  为"当日结束时 WatchEvent 累计值"，系统性偏低，仅供参考；`period_stars`
  同理是事件流记录值。
- **`desc` / `lang` / `readme` 为空**：时点元数据历史不可得。
- **`quality` 质量分级**：
  - `good`（2025-01-01 ~ 2025-10-31）：捕获率约 60%-90%，排序忠实。
    已抽查验证 DeepSeek-V3（2025-01）、agenticSeek / langflow（2025-06）
    等已知爆款在对应时段正确登顶；
  - `degraded`（2025-11-01 ~ 2026-03-31）：捕获率约 15%-60%，头部排序
    大体可信，引用具体数值需谨慎。

**断档说明：** 2026-04-01 ~ 2026-10-05 没有回填数据——事件流 Star 捕获率
降到 15% 以下，榜单失真（现象级项目几乎完全缺失，如 tester-army/e2e 真实
日增 +1,700 Star 而事件流仅剩个位数事件），回填脚本默认自动截止于
2026-03-31。另外 2026-10 实测官方 Trending 页面每日仅返回 12 条且不再支持
翻页，因此该时段的每日百名榜单已无法从任何公开渠道重建；日常抓取自
2026-10-06 起以官方页面为准继续积累。
