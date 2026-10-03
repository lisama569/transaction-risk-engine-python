# 交易风险评分与可疑行为判定服务

一套本地可运行的交易风险服务：**风险评分（可配置、可解释）→ 时间窗口可疑行为判定（去重/乱序/并发安全）→ 可疑案件处置流转（权限、幂等、版本可复现）**。
单笔与批量、直接调用与 HTTP 入口共享同一引擎实例，同一批交易重复处理得到一致结论。

- Python ≥ 3.14，FastAPI，pytest
- 纯内存存储，零外部依赖，开箱即用

## 1. 快速开始

```bash
uv sync                       # 安装依赖
uv run uvicorn main:app --reload   # 启动服务，默认 http://127.0.0.1:8000
uv run pytest                 # 运行全部测试（日志打印输入/命中规则/判定依据）
```

## 2. 模块结构

| 文件 | 职责 |
| --- | --- |
| `risk_engine/config.py` | 规则权重、金额/风险国家阈值、风险等级阈值、窗口规则、迟到容忍度；一切配置带 `version` |
| `risk_engine/models.py` | 交易、事件、告警、案件、处置结果等领域模型 |
| `risk_engine/scoring.py` | 输入校验（可区分拒绝原因）、逐条规则命中、加权评分、批量逐笔隔离 |
| `risk_engine/window.py` | 事件去重、水位迟到判定、滑动窗口计数、一次性告警、并发锁 |
| `risk_engine/cases.py` | 案件开案/快照、角色权限、幂等与并发处置、申诉重开、历史复现 |
| `risk_engine/persistence.py` | 运行状态导出/导入本地文件，规则版本校验与结构化拒绝 |
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
4. **一次性告警**：每个 `(rule_id, 维度键)` 在服务生命周期内只产生一条告警，迟到/乱序/重复补报不会二次触发。
5. **并发**：`ingest` 全程持有同一把可重入锁，水位推进、插入、计数、触发判定串行化；高并发下唯一事件恰好接收一次、告警恰好产生一次（见并发测试）。
6. 窗口状态按水位安全线定期剪枝，内存有界。

### 告警冷却与重复触发

同一 `(规则, 维度键)` 首次告警后进入**冷却期**（`alert_cooldown_seconds`，默认 3600s）：

- 冷却期内窗口再次越限**不再告警**，高密度抖动不会刷屏；
- 冷却期满（含恰好等于冷却间隔的边界）后再次越限会**重新告警**，`occurrence` 从 1 起递增，
  可通过 `GET /alerts` 看到同一模式的多次告警；
- 冷却只抑制重复告警，不影响去重、计数与水位：重复上报仍 `deduped`、超容忍迟到仍 `late_dropped`，
  任何到达顺序与并发度下告警总条数与每条结论稳定一致；
- `alert_cooldown_seconds = 0` 表示永不重复告警（兼容旧的"只报一次"语义）；
- 长跑内存有界：水位安全线以下的窗口事件与去重条目被剪枝（远古重复事件再报会判 `late_dropped`
  而非 `deduped`，同样不计数），沉寂的窗口状态整体回收，告警历史按 `max_alerts_retained`
  （默认 10000）保留最新若干条，去重表按 `max_dedup_entries`（默认 100000）上限驱逐最旧条目。

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

并发控制：案件服务全局加锁 + 终态校验，任何并发处置组合下恰好一条动作落库。

### 申诉重开（reopen）

终态案件可由有权限角色带原因重新打开，再按正常流程处置：

- **权限**：`senior_analyst` / `admin` 可重开；`analyst` 重开返回 `forbidden`；
- **前置状态**：案件须处于终态（`dismissed` / `confirmed_fraud`）；原因为空返回 `invalid_transition`；
- **幂等**：重复/并发重开，先到者 `applied=true`，其余 `duplicate` 幂等回放并带出**首次重开的原因**，多个重开理由不会混淆，历史里恰好一条重开动作；
- **不改写历史**：重开只追加一条 `to_status=open` 的历史动作并把状态置回 `open`；
  建案时固化的 `CaseSnapshot`（受理依据 + 规则版本）与既有处置历史一字不改，
  历史结论随时可按原规则版本复现；重开动作本身记录动作发生时的规则版本。

**版本策略 / 历史可复现**：

- 开案时把当时的 `规则版本 + 分数 + 全部命中明细` 固化为不可变 `CaseSnapshot`；
- 每条处置动作记录使用的规则版本；
- `upgrade_config(new_config)` 只影响之后的新评分与新处置标签；历史案件的快照与历史动作永不改变，
  随时可通过 `GET /cases/{id}` 或 `engine.reproduce_case(case_id)` 按旧版本依据复现结论。
- 测试中以 `rules-v2`（阈值/权重/窗口阈值均变化）验证旧案件结论不受影响。

## 6. 状态导出 / 导入（本地持久化）

内存运行状态可导出为本地 JSON 文件，进程重启后再导回：

```python
engine.export_state("state.json")          # 或 POST /state/export {"path": "state.json"}
result = engine2.import_state("state.json")  # 或 POST /state/import；result.ok / reason_code
```

**导出内容**（`schema_version=snapshot-v1`）：

| 字段 | 内容 |
| --- | --- |
| `rule_version` | 导出时的规则版本（导入时校验） |
| `window.deduper` | 事件去重表（event_id + 时间戳）与水位 |
| `window.states` | 各 (规则, 维度键) 的窗口事件、上次告警时间与 `occurrence` |
| `window.alerts` | 告警历史 |
| `cases` | 案件：状态、建案快照（受理依据+规则版本）、关联告警、处置历史 |

**恢复后的一致性**：

- 恢复前处理过的事件再报仍判 `deduped`，不会重复计数；
- 已报过的可疑模式在冷却期内不会因恢复再报一遍，冷却期满后重报时 `occurrence` 接着递增；
- 历史案件及其受理依据、处置历史原样可查、可复现，可继续处置/申诉重开。

**版本对不上时的行为**：导入前校验 `rule_version` 与当前配置版本，不一致**明确拒绝**
（`reason_code=version_mismatch`，说明文件版本与当前版本），引擎保持原样，
不会默默按新规则套旧状态。文件损坏/缺字段拒绝（`corrupt_snapshot`），
文件不可读拒绝（`io_error`）；所有拒绝都是结构化结果而非服务异常。
要换规则版本，请用与快照一致的版本启动恢复，或放弃快照重新累计。

## 7. HTTP 接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查与当前规则版本 |
| `POST /risk/score` | 单笔评分（成功或结构化拒绝） |
| `POST /risk/score-batch` | 批量评分，返回 `scored` 与 `rejected` |
| `POST /events` | 上报行为事件，返回 `accepted/deduped/late_dropped/alerts` |
| `GET /alerts` | 已产生的全部告警 |
| `POST /cases/{id}/open` | 以交易评分快照开案（自动关联同账户/设备告警）；交易无法评分时返回结构化拒绝 |
| `GET /cases/{id}` | 案件详情：快照、关联告警、处置历史（含版本） |
| `POST /cases/{id}/dispose` | 处置流转（角色、目标状态、原因） |
| `POST /cases/{id}/reopen` | 申诉重开（senior_analyst/admin，必须带原因） |
| `POST /state/export` | 导出运行状态到本地文件（含规则版本） |
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
uv run pytest                            # 全部 58 个用例
uv run pytest tests/test_window.py       # 窗口：重复/乱序/迟到/边界/并发
uv run pytest tests/test_window_cooldown.py  # 告警冷却与重复触发
uv run pytest tests/test_reopen.py       # 申诉重开流程
uv run pytest tests/test_persistence.py  # 状态导出/导入与恢复一致性
uv run pytest -k concurrent              # 仅并发场景
```

手工走一遍"导出 → 重启 → 恢复"：

```bash
uv run uvicorn main:app &                # 启动服务
# 打几笔事件触发告警、开一个案件并处置（见上文 curl 示例）
curl -s -X POST localhost:8000/state/export -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/risk-state.json"}'
kill %1 && uv run uvicorn main:app &     # 模拟重启
curl -s -X POST localhost:8000/state/import -H 'Content-Type: application/json' \
  -d '{"path":"/tmp/risk-state.json"}'   # {"ok":true,"reason_code":"ok",...}
curl -s localhost:8000/alerts            # 告警历史仍在，且不会因恢复重报
```

测试覆盖与可观测性：

- 评分：阈值边界（45/50/80 临界点）、规则命中明细、四类拒绝原因、批量隔离、重复评分确定性；
- 窗口：`limit` 边界（3 笔不触发/第 4 笔触发）、闭区间边界（相隔 300s/301s）、
  重复去重、容忍内乱序计数、超容忍迟到丢弃、账户/设备维度独立、
  20 线程并发恰好一次告警、16 线程重复上报同一事件恰好接收一次；
- 冷却重报：期满重报且 `occurrence` 递增、冷却边界恰好相等可重报、期内 30 笔抖动只报 1 条、
  并发两波告警恰好 2 条、长跑去重表/窗口状态有界、`cooldown=0` 兼容旧语义；
- 案件：权限矩阵、重复幂等、并发竞争一胜一冲突、版本升级后快照可复现；
- 申诉重开：权限与原因必填、重复/并发重开幂等且原因可区分、重开后快照与历史不改写、
  重开后再处置与历史复现；
- 持久化：恢复后去重/告警/案件一致、版本不符与文件损坏明确拒绝且引擎不被污染、HTTP 入口验证；
- 端到端：告警关联开案 + 并发处置；评分/事件/案件混合并发一致性；
- 每个用例通过日志打印 **输入 → 命中规则 → 判定依据/结论**（见 pytest 的 `INFO risk-tests` 输出）。

## 9. 设计取舍与边界

- 存储为进程内内存，重启清空；可用导出/导入做本地持久化（见第 6 节）。引擎实例即一致性边界，HTTP 默认使用单例。
- 窗口告警默认"冷却期后重报"（`alert_cooldown_seconds=3600`）；置 0 退化为旧的"每个规则×维度键只报一次"。
- 迟到判定基于事件时间（event time）水位，而非墙钟，便于离线补数与测试复现。
- 快照恢复是启动期操作：导入时请确保没有并发的事件上报/处置在进行。
