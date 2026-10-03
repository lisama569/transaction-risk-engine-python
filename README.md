# 交易风险评分与可疑行为判定服务

一套本地可运行的交易风险服务：**风险评分（可配置、可解释）→ 时间窗口可疑行为判定（去重/乱序/并发安全、冷却再告警）→ 可疑案件处置流转（权限、幂等、申诉重开、版本可复现）→ 运行状态导出/导入（重启可恢复）**。
单笔与批量、直接调用与 HTTP 入口共享同一引擎实例，同一批交易重复处理得到一致结论。

- Python ≥ 3.14，FastAPI，pytest
- 内存运行 + 本地文件快照，零外部服务依赖，开箱即用

## 1. 快速开始

```bash
uv sync                       # 安装依赖
uv run uvicorn main:app --reload   # 启动服务，默认 http://127.0.0.1:8000
uv run pytest                 # 运行全部测试（日志打印输入/命中规则/判定依据）
```

## 2. 模块结构

| 文件 | 职责 |
| --- | --- |
| `risk_engine/config.py` | 规则权重、金额/风险国家阈值、风险等级阈值、窗口规则、迟到容忍度、告警冷却/保留时长；一切配置带 `version` |
| `risk_engine/models.py` | 交易、事件、告警、案件、处置结果、导出/导入结果等领域模型 |
| `risk_engine/scoring.py` | 输入校验（可区分拒绝原因）、逐条规则命中、加权评分、批量逐笔隔离 |
| `risk_engine/window.py` | 事件去重、水位迟到判定、滑动窗口计数、冷却期再告警、内存剪枝、并发锁 |
| `risk_engine/cases.py` | 案件开案/快照、角色权限、幂等与并发处置、申诉重开、历史复现 |
| `risk_engine/persistence.py` | 运行状态导出/导入（JSON 快照、版本与结构校验、原子写入） |
| `risk_engine/service.py` | 统一门面 `RiskEngine`，聚合以上组件；支持配置版本升级 |
| `risk_engine/api.py` / `main.py` | FastAPI HTTP 入口，复用同一个引擎单例 |

## 3. 风险评分规则（当前版本 `rules-v1`）

评分 = 命中规则权重之和，封顶 **100**；每条命中输出 `rule_id / 名称 / 权重 / 判定依据 / 版本`，结论可解释。

| 规则 | 条件 | 权重 |
| --- | --- | --- |
| R001 大额交易 | `amount >= 10000` | 20 |
| R002 特大额交易 | `amount >= 50000`（可与 R001 同时命中） | 40 |
| R003 高风险国家 | `country ∈ {XX, ZZ}` | 30 |
| R004 整额交易 | `amount >= 1000` 且为 1000 的整数倍 | 10 |
| R005 深夜交易 | 发生时间本地小时在 `00:00–04:59` | 10 |
| R006 设备信息缺失 | `device_id` 为空/空白 | 15 |

风险等级阈值（**含边界，score ≥ 阈值归该档**）：

- `score >= 80` → `high`
- `50 <= score < 80` → `medium`
- 其余 → `low`

### 拒绝原因（绝不静默通过，四类可区分）

| `reject_reason` | 触发条件 |
| --- | --- |
| `missing_field` | 必填字段缺失：`transaction_id / account_id / amount / currency / happened_at` |
| `invalid_amount` | 金额非数值、布尔、NaN/Inf、≤ 0 |
| `invalid_time` | `happened_at` 不是时间类型 |
| `unscorable` | 币种非 3 位、国家码非 2 位字母，或评分过程异常无法出分 |

批量评分逐笔隔离：单笔被拒绝或抛异常只产生一条 `rejected` 记录，不影响其余交易出分。

## 4. 时间窗口与状态语义

默认规则：

| 规则 | 维度 | 事件 | 窗口 | 阈值 |
| --- | --- | --- | --- | --- |
| `W_ACC_PAY` | 账户 `account_id` | payment | 300s | 窗口内 > 3 笔触发 |
| `W_DEV_LOGIN` | 设备 `device_id` | login | 600s | 窗口内 > 4 次触发 |

语义约定：

1. **去重**：以 `event_id` 全局去重。重复上报返回 `deduped=true`，不计数、不触发。
2. **水位与迟到**：维护已见事件最大时间戳 `watermark`；当 `occurred_at < watermark - late_tolerance_seconds`（默认 3600s）判为迟到，`late_dropped=true` 丢弃；容忍区间内的**乱序事件按时间戳插入**并正常计数。
3. **滑动窗口（闭区间）**：每来一个事件，窗口为 `[t-window, t]`，窗口内去重事件数 **严格大于 limit** 才触发；恰好相隔一个窗口长度仍算同窗口（边界已测）。
4. **冷却与再告警**：同一 `(rule_id, 维度键)` 触发告警后进入冷却期
   （`alert_cooldown_seconds`，默认 900s，**按触发事件的 `occurred_at` 起算，含边界**）：
   冷却期内的越限事件被抑制、不刷屏；冷却结束后窗口再次越限则**再次告警**，
   相邻两次告警间隔至少一个冷却期。重复上报、乱序补报、迟到数据依旧不计数、不凭空触发。
5. **并发**：`ingest` 全程持有同一把可重入锁，水位推进、插入、计数、触发判定串行化；高并发下唯一事件恰好接收一次、同一波越限恰好告警一次（见并发测试）。
6. **内存有界**：去重集合只保留水位容忍区间内的 `event_id`（更早的重复按迟到丢弃，结论相同）；
   窗口事件按水位安全线剪枝；冷却已过且窗口已空的 `(规则, 键)` 状态被丢弃；
   历史告警按 `alert_retention_seconds`（默认 86400s，相对水位）剪枝。长跑内存不随告警数无限增长。
7. **结构化拒绝**：事件字段缺失/为空（`missing_field`）或时间非法（`invalid_time`）时返回
   `reject_reason/reject_detail`，不会变成服务异常。

## 5. 案件处置：权限、幂等与版本策略

合法流转：`open → dismissed | confirmed_fraud`（终态不可再流转）。

| 角色 | 可流转目标 |
| --- | --- |
| `analyst` | `dismissed` |
| `senior_analyst` | `dismissed`, `confirmed_fraud` |
| `admin` | 全部 |

处置返回 `reason_code`，**重复与并发冲突原因可区分**：

| reason_code | 含义 |
| --- | --- |
| `ok` | 首次处置成功，写入一条历史动作 |
| `duplicate` | 目标状态即当前状态：重复提交/并发中同目标后到者，幂等回放，不重复落库 |
| `concurrent_conflict` | 案件已被并发处置为**另一**终态，本次竞争失败 |
| `forbidden` | 角色无权流转到目标状态 |
| `not_found` | 案件不存在 |
| `invalid_transition` | 非法状态流转 |
| `missing_reason` | 申诉重开未携带非空原因 |

并发控制：案件服务全局加锁 + 终态校验，任何并发处置组合下恰好一条动作落库。

### 申诉重开（reopen）

- 终态案件（`dismissed` / `confirmed_fraud`）可由 **`senior_analyst` 或 `admin`** 携带**非空原因**重开为 `open`，
  之后按正常流程再次处置；`analyst` 无权重开（`forbidden`）。
- 重开只**追加**一条 `to_status=open` 的历史动作：建案时固化的 `CaseSnapshot`（规则版本+分数+命中明细）
  与全部历史动作**永不修改**，历史结论随时可按旧版本依据原样复现。
- 幂等与并发：重复/并发重开只有第一次生效，其余得到 `duplicate` 幂等回放（回放首次重开人与原因，
  原因可区分）；未处于终态时重开返回 `invalid_transition`；案件不存在返回 `not_found`；
  原因为空返回 `missing_reason`。

**版本策略 / 历史可复现**：

- 开案时把当时的 `规则版本 + 分数 + 全部命中明细` 固化为不可变 `CaseSnapshot`；
- 每条处置动作记录使用的规则版本；
- `upgrade_config(new_config)` 只影响之后的新评分与新处置标签；历史案件的快照与历史动作永不改变，
  随时可通过 `GET /cases/{id}` 或 `engine.reproduce_case(case_id)` 按旧版本依据复现结论。
- 测试中以 `rules-v2`（阈值/权重/窗口阈值均变化）验证旧案件结论不受影响。

## 6. 状态导出 / 导入（重启恢复）

`engine.export_state(path)` 把当前运行状态原子写入本地 JSON 文件（先写临时文件再 `os.replace`，
不会留下半截文件）；`engine.import_state(path)` 在启动时恢复。HTTP 入口为
`POST /state/export` 与 `POST /state/import`，入参均为 `{"path": "..."}`。

**快照文件内容**：

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 快照结构版本（当前为 1） |
| `rule_version` | 导出时的规则版本 |
| `exported_at` | 导出时间（UTC，ISO 格式） |
| `behavior` | 事件去重集合、水位、各 `(规则, 维度键)` 窗口状态（含冷却基线 `last_alert_at`）、告警历史 |
| `cases` | 全部案件：状态、固化的评分快照、关联告警、处置/重开历史 |

**恢复后的一致性**：

- 之前处理过的事件仍算重复（去重集合随水位一起恢复）；
- 已报过的可疑模式不会因恢复重复报（冷却基线与告警历史一起恢复，冷却期内仍被抑制）；
- 历史案件与建案依据原样可查，恢复后可继续处置或申诉重开。

**版本对不上时的行为**：导入前逐项校验，任何一项不符都**明确拒绝且当前运行状态保持不变**
（先在新实例上完整校验，全部通过才整体替换）：

| `reason_code` | 含义 |
| --- | --- |
| `not_found` | 快照文件不存在 |
| `corrupt_file` | 文件无法读取或不是合法 JSON |
| `schema_mismatch` | 快照结构版本与引擎不支持，或内容无法按当前规则语义解析 |
| `version_mismatch` | 文件中的 `rule_version` 与当前配置不一致——**拒绝导入，绝不默默按新规则套改老结论** |

## 7. HTTP 接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查与当前规则版本 |
| `POST /risk/score` | 单笔评分（成功或结构化拒绝） |
| `POST /risk/score-batch` | 批量评分，返回 `scored` 与 `rejected` |
| `POST /events` | 上报行为事件，返回 `accepted/deduped/late_dropped/alerts` |
| `GET /alerts` | 已产生的全部告警 |
| `POST /cases/{id}/open` | 以交易评分快照开案（自动关联同账户/设备告警） |
| `GET /cases/{id}` | 案件详情：快照、关联告警、处置历史（含版本） |
| `POST /cases/{id}/dispose` | 处置流转（角色、目标状态、原因） |
| `POST /cases/{id}/reopen` | 申诉重开终态案件（`senior_analyst`/`admin`，原因必填） |
| `POST /state/export` | 导出运行状态到本地文件 `{"path": "..."}` |
| `POST /state/import` | 从本地文件恢复状态；版本/结构不符返回结构化拒绝 |

示例：

```bash
curl -s -X POST localhost:8000/risk/score -H 'Content-Type: application/json' -d '{
  "transaction_id":"t1","account_id":"a1","amount":60000,"currency":"USD",
  "device_id":"d1","happened_at":"2026-09-23T03:00:00","country":"XX"}'

curl -s -X POST localhost:8000/events -H 'Content-Type: application/json' -d '{
  "event_id":"e1","account_id":"a1","device_id":"d1",
  "event_type":"payment","occurred_at":"2026-09-23T10:00:00"}'
```

## 8. 本地验证方法

```bash
uv run pytest                          # 全部 59 个用例
uv run pytest tests/test_window.py     # 窗口：重复/乱序/迟到/边界/并发
uv run pytest tests/test_realert.py    # 冷却与再告警、抖动不刷屏、内存有界
uv run pytest tests/test_reopen.py     # 申诉重开：权限/幂等/并发/历史不变
uv run pytest tests/test_persistence.py# 导出/导入：恢复一致性、版本与结构校验
uv run pytest -k concurrent            # 仅并发场景
```

手动验证重启恢复（两条终端）：

```bash
# 1) 启动服务，上报事件触发告警、开案并处置
uv run uvicorn main:app
curl -s -X POST localhost:8000/state/export -H 'Content-Type: application/json' \
  -d '{"path": "/tmp/risk-state.json"}'
# 2) 重启进程后导入，之前的事件仍算重复、告警不重复报、案件照旧可查
curl -s -X POST localhost:8000/state/import -H 'Content-Type: application/json' \
  -d '{"path": "/tmp/risk-state.json"}'
```

测试覆盖与可观测性：

- 评分：阈值边界（45/50/80 临界点）、规则命中明细、四类拒绝原因、批量隔离、重复评分确定性；
- 窗口：`limit` 边界（3 笔不触发/第 4 笔触发）、闭区间边界（相隔 300s/301s）、
  重复去重、容忍内乱序计数、超容忍迟到丢弃、账户/设备维度独立、
  20 线程并发恰好一次告警、16 线程重复上报同一事件恰好接收一次；
- 冷却再告警：冷却后再触发、冷却边界（恰好到期可再告警）、冷却期内高密度抖动不刷屏、
  相邻告警间隔 ≥ 冷却期、冷却后并发一波恰好再告警一次、长跑内存有界；
- 案件：权限矩阵、重复幂等、并发竞争一胜一冲突、版本升级后快照可复现；
- 申诉重开：角色权限、终态校验、原因必填、重复/并发幂等且原因可区分、重开后快照与历史不变；
- 持久化：导出/导入后去重与告警不重报、案件与快照可查、版本不匹配/结构不符/文件损坏均
  结构化拒绝且当前状态不变、HTTP 入口同样行为；
- 端到端：告警关联开案 + 并发处置；评分/事件/案件混合并发一致性；
- 每个用例通过日志打印 **输入 → 命中规则 → 判定依据/结论**（见 pytest 的 `INFO risk-tests` 输出）。

## 9. 设计取舍与边界

- 存储为进程内内存 + 本地文件快照：导出/导入需显式触发（或挂在启停钩子上），
  快照不是实时落库，崩溃可能丢失最近一次导出之后的增量。
- 引擎实例即一致性边界，HTTP 默认使用单例；多实例部署需要外部分片/锁，不在本服务范围内。
- 冷却期按事件时间（`occurred_at`）起算而非墙钟，与水位语义一致，便于离线补数与测试复现。
- 迟到判定基于事件时间（event time）水位，而非墙钟，便于离线补数与测试复现。
- 导入严格要求 `rule_version` 一致：跨版本迁移需要显式的迁移工具，而不是静默套用新规则。
