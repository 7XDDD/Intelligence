# 纳斯达克情报系统 · 代码审查与修正交付说明

> 依据《Nasdaq Intelligence MVP Code Review and Action Plan》执行。审查对象为服务器 `/opt/nasdaq-intel` 实际运行代码（非任务书 ZIP 中的 FMP 工程），所有结论均有文件/函数依据。

## 1. 审查范围与实际工作流

- 线上入口：`http://47.94.225.66:8040`（systemd `nasdaq-intel-web.service`）
- 实际入口：[run.py](../run.py)
- 工作流：市值排名 → 行情采集 → 新闻/公告采集 → 去重 → LLM 分类评分 → 深度分析 → 趋势总结 → Markdown 日报 → JSON/JSONL/SQLite 归档
- 任务书 ZIP 中的 `market_data.py`、FMP Key、Apple 单公司报告**不在当前 8040 项目**；服务器全盘检索未发现 `FMP_API_KEY`。

## 2. 差距分析与处理

| 任务书要求 | 改造前 | 现状 | 处理 |
|---|---|---|---|
| 盘中价/收盘价分离 | 无行情模块，报告无市场表现 | 新增 [market.py](../nasdaq_intel/sources/market.py)，字段拆分 | ✅ |
| 时区与交易日 | 依赖服务器 `date.today()`，周末/节假日无处理 | [market_calendar.py](../nasdaq_intel/market_calendar.py)，美东交易日 + 北京时间展示 + cron 守卫 | ✅ |
| API 重试/错误分类 | urllib3 默认退避，无永久/临时分类 | [http.py](../nasdaq_intel/http.py) 2/4/8s 退避、`Retry-After`、`HttpError.permanent` | ✅ |
| 数据质量校验 | 仅排名候选池提示 | [quality.py](../nasdaq_intel/quality.py)，排名/材料/行情/报告结构四类校验 | ✅ |
| 动态 Top10 名单 | 已实现 | 保留 | ✅ |
| 新闻/公告/去重/分类/评分/分析/来源 | 已实现 | 保留并补充 topics 十维标签、资本市场预期 | ✅ |
| SQLite 历史存储 | 仅 JSON 文件 | [db.py](../nasdaq_intel/db.py)：名单/材料/事件/行情/运行/LLM 调用 | ✅ |
| 日报六段结构 | 旧版六段结构 | [report.py](../nasdaq_intel/pipeline/report.py) 对齐任务书 7.5 | ✅ |
| 成本可观测 | 仅 Token 日志 | `run.py stats` + LLM 单价配置 + 审计日志 | ✅ |
| 公网访问控制 | 完全公开只读 | 可选 HTTP Basic Auth（环境变量），无管理后台 | ✅ |
| FMP Key 轮换 | 无法代为操作 | 当前项目无 FMP Key；ZIP 中 Key 需在 FMP 后台轮换 | ⚠️ 待用户执行 |
| 连续 7 天验证 | — | 已提供 `stats`，当前仅 2 个报告日 | ⚠️ 需时间积累 |

## 3. 关键口径（P0）

- `current_price`：行情快照最新价，附 `quote_timestamp` 与 `market_status`。
- `previous_close`：上一交易日收盘价（历史日线）。
- `daily_close`：报告日（美东交易日）历史日线收盘价；未收盘时为 `null`，报告中显示「未收盘」。
- 历史日期重跑：不展示当前盘中价，只展示该交易日收盘价（`historical=true`）。
- 时间：采集与日志用 UTC；交易日期用 `America/New_York`；日报展示用 `Asia/Shanghai`。

## 4. SQLite 结构（`data/intelligence.db`）

| 表 | 主键 | 内容 |
|---|---|---|
| companies | (snapshot_date, ticker) | 每日 Top10 名单与市值 |
| materials | id | 新闻/SEC 原始材料，含 URL、发布时间 |
| events | (report_date, event_key) | 事件、分类、主题、重要性、来源、分析 JSON |
| market | (report_date, ticker) | 最新价/前收/收盘/涨跌/快照时间 |
| runs | run_id | 运行状态、节点、错误、报告路径 |
| llm_calls | request_id | 模型、耗时、Token、成本、状态 |

旧库启动时自动迁移：events 主键由 `event_key` 改为 `(report_date, event_key)`，并补充 `topics_json`。

## 5. Prompt 文档

- [classify.txt](../prompts/classify.txt)：5 个粗分类 + 10 个主题标签 + 1–5 评分 + JSON 契约 + 注入防御。
- [analyze.txt](../prompts/analyze.txt)：事实/推断/假设分离，新增「资本市场预期」。
- [trend.txt](../prompts/trend.txt)：跨公司技术、竞争、资本信号、观察项。

## 6. 测试与验收

- 单元测试：`python -m unittest discover -s tests`，共 46 项通过。
- 覆盖：交易日历、行情口径、盘中/历史报告、质量校验、SQLite 迁移、报告结构、HTTP 错误分类、Token 成本、去重、分类、审计日志。
- 线上验收：10/10 公司、行情 10 家、事件来源可追溯 100%、LLM 调用全部 `ok`、`/health` 200。
- 日报样例：`data/reports/2026/11/2026-11-02.md`；历史收盘样例：`data/reports/2026/10/2026-10-30.md`。

## 7. 部署与回滚

- 部署：上传代码包 → 解压至 `/opt/nasdaq-intel` → 运行测试 → 执行日报 → 重启 `nasdaq-intel-web.service`。
- 代码备份：`/root/nasdaq-intel-code-backup-20261102-235636.tgz`；另有 `/opt/nasdaq-intel-backup-*`。
- 数据保护：部署包不包含 `data/`、`.env`、`.venv`；历史日报与 SQLite 不受影响。
- 回滚：解压上述备份覆盖 `/opt/nasdaq-intel` 后重启服务；数据库迁移为向前兼容，可保留。
- 密钥：`.env` 权限 600，未进入日志、代码或部署包。

## 8. 监控与成本

- `python run.py stats --days 7`：运行成功率、公司数、事件数、Token、成本。
- `python run.py audit --date YYYY-MM-DD --full`：逐条复核模型原始输入/输出。
- 成本：在 `config/settings.yaml` 配置 `llm.input_cost_per_million` / `output_cost_per_million` 后自动估算。

## 9. 未完成 / 后续

1. FMP Key 轮换（不在本项目管理范围，需用户在 FMP 后台执行）。
2. 连续 7 天稳定性观察（当前仅积累 2 个报告日）。
3. TTM 与财报连续性校验：当前项目未接入财务报表模块，暂不适用。
4. 公司基本面档案：保留为后续独立模块，不与每日情报日报合并。
5. 去重粒度可进一步从「按公司分桶」演进为「按文章事件」，以处理一稿覆盖多家公司财报的情况。
