# GitHub 趋势分析落地方案

> 状态：阶段 1–4 准备已部署至 main；30 天观测待首个有效线上 Star 快照启动
> 编制基线：2026-10-13
> 适用范围：本仓库 GitHub Trending 日常采集、仓库 Star 追踪、静态看板与相关 GitHub Actions

## 1. 目标与明确不做的事

### 目标

第一版解决两个彼此独立的问题：

1. **热点是否持续出现在官方榜单？** 统计官方 Trending 中的新上榜、重复上榜、可比较的排名变化与实际采集覆盖。
2. **上榜项目是否持续增长？** 对官方榜单发现的仓库定期记录 GitHub API 返回的总 Star，并在观测足够时计算约 7 日、30 日 Star 差值。

### 不属于第一版的范围

- 不把每天的榜单强行补齐为 100 条。GitHub 官方页面实测每天约返回 8–13 条，应忠实记录实际返回数。
- 不把 Trending 样本解释为 GitHub 全站项目或语言的代表性样本。
- 不把 GH Archive 历史回填、GitHub Trending 与 Search API 结果混成一条趋势或同一排行榜。
- 不以 Trending 页面上的 `period_stars` 代替连续 Star 观测。
- 不引入难以解释的综合“热度分数”，也不在第一版建立 Search API 全站候选池。

## 2. 当前项目基线与需先修正的问题

项目现有数据和入口：

- 日报/月报抓取与快照：[`scripts/fetch_trending.py`](../scripts/fetch_trending.py)
- 看板聚合：[`scripts/build_dashboard.py`](../scripts/build_dashboard.py)
- 每日自动化：[`.github/workflows/trending.yml`](../.github/workflows/trending.yml)
- 快照与历史数据口径说明：[`analysis/README.md`](../analysis/README.md)
- 看板页面：[`dashboard/index.html`](../dashboard/index.html)
- 回归测试：[`tests/test_fetch_trending.py`](../tests/test_fetch_trending.py)、[`tests/test_build_dashboard.py`](../tests/test_build_dashboard.py)

以编制基线日为准，近期官方快照每日有 8–13 条；历史文件还包含 GH Archive WatchEvent 回填，且存在质量分级与时间断档。执行前若这些情况已变化，应重新核实，但不得因此改变来源隔离规则。

实施时先处理两个与趋势解读直接相关的问题：

1. 看板聚合目前按全部快照累计语言、出现天数和峰值。官方 Trending 与 GH Archive 的排序与数值口径不同，应拆分来源统计，回填数据另按 `good` / `degraded` 质量等级展示。
2. 看板每日条目默认截取前 12 条，近期 13 条的官方榜单会有一条未展示。官方榜单应显示实际全部条目；截取若仍用于历史回填，只能影响显示，不能误称为完整统计。

## 3. 统计口径与业务规则

### 3.1 官方榜单趋势

- 每日以官方 Trending `daily` 页面为来源，记录页面实际返回数及完整顺序。
- 上榜天数、新上榜、重复上榜、语言分布均只在官方快照中计算。
- `period_stars` 仅表示当次页面提供的周期值；作为榜单快照的原始字段保存，不用它推导缺失日期的每日增长。
- 7 日 / 30 日榜单窗口的分母是窗口内**有效的官方快照日期数**。缺失快照不记为“未上榜”。
- 仅项目在相邻的有效官方快照中均出现时，才计算名次变化；不在榜不代表 Star 为零或热度归零。
- 语言占比的表述必须限定为“官方 Trending 榜单条目中的语言分布”。

### 3.2 Star 追踪候选集

- 候选来源仅为最近 30 个日历日内出现过的官方 `daily` 榜单仓库，含采集日及此前 29 天。
- 以 `owner/repo` 全名去重；记录每个仓库最近一次官方上榜日期 `last_seen`。
- 仓库停止上榜后仍追踪至最后上榜日期满 30 天；过期仓库不再进入次日候选集。
- GH Archive 回填及 Search API 年报项目不得自动进入候选集。
- 第一版不追溯 Star 基线：首次有效 API 查询建立首个观测点，不推算此前增量。

按近期每天最多约 13 条估计，30 个日历日的保守候选上限约为 390 个不同仓库（实际通常更低，因为会重复上榜）。实施前需在 CI 环境实测调用耗时和 API 剩余额度；不把这个估算视作配额保证。

### 3.3 Star 差值窗口

输入是 GitHub 仓库 API 返回的累计 `stargazers_count`，不是榜单 `period_stars`。

- 近 7 日：以最新有效观测日期为结束点，寻找距结束点 6–8 个日历日的有效基线。
- 近 30 日：寻找距结束点 27–33 个日历日的有效基线。
- 差值为“结束观测 Star - 基线 Star”，允许为负数；不得把负值钳制为 0。
- 展示结束日期、基线日期、实际间隔天数及有效观测状态。
- 找不到符合窗口的基线时显示“数据不足”，不显示 0、不插值、不外推。
- 失败的 API 查询不得作为 0 或有效观测参与计算。

## 4. 数据设计

现有 `data/YYYY-MM-DD.json` 保持原格式。Star 观测单独存放，避免被当前榜单聚合器误读：

```text
data/
  YYYY-MM-DD.json
  repo-stars/
    YYYY-MM-DD.json
```

每日 Star 文件建议结构：

```json
{
  "schema_version": 1,
  "date": "YYYY-MM-DD",
  "fetched_at": "带时区的 ISO 8601 时间",
  "source": "github-repos-api",
  "candidate_policy": "official-trending-last-seen-30d",
  "candidate_count": 12,
  "attempted": 12,
  "http_requests": 13,
  "succeeded": 11,
  "failed": 1,
  "not_attempted": 0,
  "repos": [
    {
      "name": "owner/repo",
      "stars": 1234,
      "status": "ok",
      "last_seen": "YYYY-MM-DD"
    },
    {
      "name": "owner/removed-repo",
      "stars": null,
      "status": "not_found",
      "last_seen": "YYYY-MM-DD"
    }
  ]
}
```

规则：

- `stars` 在 `status == "ok"` 时为非负整数；其他状态为 `null`。
- 至少区分 `ok`、`not_found`、`forbidden`、`rate_limited`、`network_error`、`invalid_response`、`not_attempted`。若 GitHub 返回的状态不足以可靠区分 403 与额度限制，按响应头/错误消息识别；无法确认时保留更一般的失败状态并记录错误类别，不猜测。
- `candidate_count` 为唯一候选仓库总数；`attempted` 为至少发起过一次 API 查询的仓库数；`http_requests` 为实际 HTTP 请求总数（包含重试）；`succeeded` 为 `ok` 数；`failed` 为已尝试但未成功数；`not_attempted` 为未发起查询数。计数必须满足 `candidate_count == attempted + not_attempted`、`attempted == succeeded + failed` 及 `http_requests >= attempted`。未请求条目应写为 `not_attempted`，并明确记录，不能静默丢弃。
- 每个仓库最多保留一条记录；`last_seen` 来自官方日榜。
- 不将 token、Authorization 头或未经清理的敏感响应内容写入文件或日志。
- 对完整快照先写临时文件，再原子替换目标路径。无法生成完整合法 JSON 时不得覆盖已有文件。
- 如果 API 整体不可用，仍需以明确的失败状态记录已知候选的未成功观测；不能制造成功快照。工作流应保留日报，并显式报告 Star 追踪步骤异常。

## 5. 实施阶段、任务与验收

估算按一名熟悉本仓库的开发者计算，包含实现和测试，不含阶段 4 的观察等待时间。建议逐阶段合并和验收，避免一次性引入口径、抓取和 UI 多种变化。

### 阶段 1：来源隔离与榜单完整展示（已实施；原估算 1–1.5 人日）

**改动范围：** `scripts/build_dashboard.py`、`tests/test_build_dashboard.py`、`dashboard/index.html`。

任务：

1. 在聚合结果中按来源和质量等级隔离官方数据、`gharchive-watchevent/good`、`gharchive-watchevent/degraded`。
2. 页面默认聚焦官方区；历史回填另区展示，并清楚标注来源、质量和不可直接比较的边界。
3. 官方快照列表展示实际全部返回条目，不受当前 `top-items=12` 截断。
4. 如历史回填列表仍限制显示条数，页面须区分“展示条数”和“统计条数”；既有回填统计继续使用完整快照，不因显示截断改变。
5. 确认官方语言分布、累计上榜天数、单日峰值仅由官方数据计算，历史回填指标不混入。

**验收条件：**

- 混合官方与两种回填质量级别的测试数据产生相互独立的汇总。
- 13 条官方条目全部存在于看板数据和页面；8 条、12 条也不补齐。
- 回填条目的展示截断不会影响按完整快照计算的统计。
- 看板标题、说明及统计范围不再暗示跨来源可比。
- `tests/test_build_dashboard.py` 全部通过，聚合重复运行幂等。

**实施记录（2026-10-13）：** 聚合结果已按官方 Trending、GH Archive good/degraded 分组；看板提供来源切换并在当前来源内计算指标。官方榜单不再受 `--top-items` 截断，历史回填仍按该参数截断显示，但条数、Star 峰值、语言分布与累计统计使用完整数据。回归测试覆盖来源隔离、13 条以上官方条目完整展示、回填截断及全量统计；`uv run --with pytest --with requests --with beautifulsoup4 pytest -q tests/test_build_dashboard.py` 通过。

### 阶段 2：每日 Star 采集与可靠落盘（已实施；原估算 1.5–2.5 人日）

**改动范围：** 新增 `scripts/track_stars.py` 及相应测试；调整 `.github/workflows/trending.yml`；更新 `README.md` 或 `analysis/README.md`。

任务：

1. 读取最近 30 个日历日的官方快照，生成去重候选集及 `last_seen`。
2. 调用 GitHub Repositories API，使用 Actions 提供的 `GITHUB_TOKEN`；本地运行时若缺少 token，应给出明确错误/提示且不得写成成功快照。
3. 记录 HTTP 状态、失败类别和响应头所需的配额信息；采用有限重试与退避，避免对不可恢复的 4xx 无意义重试。
4. 对配额不足、次级限流或运行预算不足的情况停止新增请求，将剩余仓库记录为 `not_attempted`，同时以非成功覆盖率告警；预算不足时按最近上榜日期优先查询。
5. 写入 `data/repo-stars/YYYY-MM-DD.json`，校验计数关系及每条记录，再原子替换。
6. 将 Star 跟踪作为独立 Actions 步骤，保证辅助采集失败不会丢弃已生成的日报；同时失败必须在日志/通知中可见，不能静默降级。
7. 将 `data/repo-stars/` 加入现有自动提交步骤。
8. 观测候选数、成功数、失败数、请求耗时、总耗时和剩余额度；任何密钥不得进入日志或数据文件。

**调用量与耗时控制：**

- 初始候选上限按 30 天滚动窗口估算，约 390 个不同仓库；HTTP 请求预算为最多 400 次，包含重试。
- 设置显式的请求/运行预算上限；达到上限后清楚记录未请求候选，不隐式缩减集合。
- 只有实测证明当前 10 分钟 workflow 超时预算不足时，才调整 `timeout-minutes`，并以最大候选集和有限重试进行验证。

**验收条件：**

- 测试覆盖 API 成功、404、403、429、5xx 重试耗尽、网络错误、配额中断、缺少 token、空候选集和重复候选。
- `stars=0` 与 `stars=null` 明确区分；失败值不参与增长计算。
- `candidate_count == attempted + not_attempted` 且 `attempted == succeeded + failed`；不得出现静默缺项。
- 只从官方快照生成候选，不从 GH Archive 或 Search API 年报纳入。
- CI 全程使用 mock，不发真实 GitHub 请求。
- Star 跟踪故障时日报仍可提交，Actions 对追踪失败有可检索告警。

**实施记录（2026-10-13）：** 新增 `scripts/track_stars.py`，按最近 30 个日历日的官方日榜去重追踪候选，仅采集仓库 API 总 Star；写入独立 `data/repo-stars/YYYY-MM-DD.json`，包含逐仓库状态、候选数、仓库尝试数、HTTP 请求数、剩余额度和时区时间戳，并校验 schema/计数后原子替换。预算不足时优先查询最近上榜的仓库。默认最多 400 次 HTTP 请求（含重试）、最多运行 480 秒；遇到限流、403 或预算耗尽即停止并标记未请求候选。工作流将其作为 `continue-on-error` 步骤运行，追踪不完整会发出 Actions warning 并尝试复用已配置的 webhook 告警，日报仍继续构建和提交。README 与 `analysis/README.md` 已补充用法和口径。测试覆盖来源过滤、日期校验、零 Star、404/403/429、5xx 重试、网络/响应错误、预算停止、token、安全落盘及空候选集。

### 阶段 3：趋势计算与看板（1.5–2.5 人日）

**改动范围：** `scripts/build_dashboard.py`、`tests/test_build_dashboard.py`、`dashboard/index.html`。

增加两个隔离视图：

1. **官方榜单趋势**：实际采集条数、有效快照日期数、新上榜项目、反复上榜项目、相邻有效榜单间可比较的排名变化。
2. **仓库 Star 趋势**：仓库累计 Star 时间序列、近 7/30 日差值、实际观测间隔、基线和数据覆盖状态。

每一项统计必须附带或可追溯到来源、窗口、有效日期/观测数；缺失窗口、API 失败和样本不足均显示为“数据不足/未观测”，而不是 0。页面不把“未上榜”解释成“没有增长”。

**实施记录（2026-10-13）：** 看板聚合增加 `official_trends`，只读取官方 cohort 的完整榜单，比较相邻有效快照并记录实际条数、间隔、新上榜项目、重复上榜数及共同上榜项目的名次变化。7/30 日摘要按日历窗口给出实际有效快照覆盖；页面列出最近 14 个有效日及对比基线日期。Star 聚合读取 `data/repo-stars/*.json`，校验 schema、来源、候选策略、仓库状态与数值，仅展示最近 30 个日历日官方榜单候选；7/30 日变化从 API `status=ok` 观测计算，窗口分别允许 6–8 日、27–33 日基线，保留负差，并显示基线/结束日期、实际间隔及区间有效观测覆盖。没有有效基线时输出“数据不足”，不插值、不填零。看板新增官方榜单趋势表、仓库选择器、累计 Star 时间序列及近期状态表；GH Archive 仍保持独立视图。测试覆盖来源隔离、窗口边界、缺失日期、失败观测、负差、损坏快照及重复生成。实现没有调用真实 GitHub API；阶段 4 的在线采集效果与覆盖率复核仍待试运行。

**验收条件：**

- 7/30 日窗口边界测试覆盖最早/最晚允许基线、窗口外基线、缺失日和失败观测。
- 有效基线时差值精确等于两次累计 Star 的差；负差值保留。
- UI 显示基线日期、结束日期、实际间隔与区间有效观测覆盖；数据不足不渲染成 0。
- 官方 Trending 与 GH Archive 指标在数据结构和 UI 均不可混算。

### 阶段 4：上线试运行与复核（实现 0.5–1 人日；观测 30 天）

1. 每日 Actions 顺序为：官方日报抓取 → Star 追踪 → 看板聚合 → 提交报告、快照和看板。
2. Star 跟踪作为可告警的辅助步骤；失败不得阻断日报提交，但也不得被当作通过或成功覆盖。
3. 首周人工抽查 API 返回的仓库与 Star 数，核对错误状态和重复记录。
4. 连续观察 30 天，记录每日候选数、成功率、配额余量、耗时和未观测日期。
5. 满 30 天后复核 7 日指标覆盖；满约 33 天后复核 30 日指标基线覆盖。
6. 只有在实际分析价值、API 额度和运行时间都达标后，才考虑扩大候选来源或追踪规则；第一版不自动扩展为 Search API 全站采样。

**实施与启动记录（2026-10-13）：** 新增 `scripts/review_star_tracking.py`，可按指定结束日期汇总任意日历窗口的快照覆盖、缺失/无效日期、候选数、请求数、成功率、失败/未请求日期、运行耗时及 API 剩余额度；无观测时比率/额度/耗时返回 `null`，不以零伪装数据。`scripts/track_stars.py` 新生成的快照持久化 `duration_seconds`，供事后复盘。运行示例：`uv run python scripts/review_star_tracking.py --end-date YYYY-MM-DD --window-days 30`。阶段 1–4 准备已于 commit `e870ea6` 推送到 `main`，GitHub Tests workflow 已通过；Trending Reports workflow 仅由定时计划或手动 dispatch 触发，push 不会启动采集。30 天观测尚未开始，起始日以首个有效线上 Star 采集快照日期为准；首次真实请求、API 配额/运行耗时抽查和连续 30 天覆盖复核仍待完成。

**最终验收条件：**

- 最近 30 天官方快照无来源混算，实际条目数透明。
- Star 数据连续、状态可解释，7/30 日指标均能说明有效覆盖程度。
- 30 日试运行有覆盖率、失败率、耗时、额度余量的复盘结果。
- 若任一核心指标覆盖不足，报告不足原因并调整采集规则，不以填零或插值制造完整趋势。

## 6. 依赖关系与关键路径

```text
阶段 1：隔离来源与完整展示
  └── 阶段 2：采集并保存连续 Star 观测
        └── 阶段 3：计算/展示增长与榜单趋势
              └── 阶段 4：30 天试运行和指标复核
```

阶段 2 的采集实现可以与阶段 1 的 UI 工作并行开发，但阶段 3 必须依赖阶段 1 的来源隔离和阶段 2 的稳定快照格式。试运行应在阶段 1–3 验收、部署到默认分支后开始。

## 7. 风险、控制与回滚

| 风险 | 影响 | 控制措施 |
|---|---|---|
| Trending 页面改版或条数继续变化 | 采集解析失败或榜单变短 | 原样记录实际条数；空解析沿用现有失败路径；测试 8/12/13 条与解析失败。 |
| GitHub API 限流或网络故障 | Star 观测缺失、窗口覆盖下降 | 有限重试、检查配额、记录 `not_attempted`，明确报告覆盖；日报与辅助采集解耦。 |
| 不同来源被合并解读 | 得出错误的语言/热度趋势 | 计算层按来源隔离，UI 分区，测试混合输入。 |
| 仓库被删除、私有化或 API 不可访问 | Star 时间序列出现空值 | 记录失败状态及日期，不写 0；不参与差值。 |
| 全量每日快照增长 | 仓库体积和看板构建时间增长 | 首版只保存候选仓库每日观测；监测 JSON 体积和 workflow 时长，评估后再决定是否压缩/归档。 |
| 新指标误导读者 | 将短期样本误认为长期趋势 | 显示有效样本量、日期和口径；不足时不画趋势、不填零；至少运行到对应窗口完整后再发布指标。 |

**回滚原则：**

- 每个阶段单独合并；问题可通过回滚该阶段代码/工作流步骤停止新行为。
- 停止 Star 工作流步骤不会删除既有 `data/repo-stars/`；保留历史观测以便恢复。
- 不改写现有日报快照或历史回填文件；若看板生成失败，保留已提交的原始数据和上次有效看板，不提交不完整聚合产物。
- API token 继续使用 GitHub Actions secret，不写入仓库配置、数据或日志。

## 8. 验证命令与执行清单

本仓库当前建议的测试入口：

```bash
uv run --with pytest --with requests --with beautifulsoup4 pytest -q
```

新增追踪测试后，应继续使用 mock 响应，不要求 CI 网络访问 GitHub。各阶段至少执行相关测试文件；阶段合并前执行完整测试入口。

上线前逐项确认：

- [ ] 看板官方榜单完整展示，官方/回填统计隔离。
- [ ] 数据 schema、状态值、计数关系和候选窗口规则已由测试覆盖。
- [ ] `GITHUB_TOKEN` 仅通过环境变量注入，日志无敏感值。
- [ ] Actions 辅助步骤失败不丢日报，失败状态可见且可告警。
- [ ] 仓库 API 请求数、耗时、配额与 `timeout-minutes` 经实际运行验证。
- [ ] Star 差值不跨来源、不跨无效窗口、不将缺失转换为零。
- [ ] 看板趋势区显示日榜采集覆盖、比较基线、Star 差值区间与仓库观测覆盖。
- [ ] README 或 `analysis/README.md` 已说明新的数据目录、口径和运行方式。
- [ ] 30 天试运行开始日期、覆盖率与复盘结论已记录。
- [ ] `review_star_tracking.py` 复盘摘要已核对无效快照、缺失日期、成功率、运行耗时和 API 配额。
