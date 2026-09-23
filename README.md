# 交易风险评分与可疑行为判定服务

本地可运行的交易风险引擎，提供三类能力：

1. **单笔 / 批量交易风险评分**：规则与权重可配置，结论可解释；非法输入绝不静默通过，返回可区分的拒绝原因；批量中单笔失败互不影响。
2. **时间窗口可疑行为判定**：账户、设备两个维度，正确处理迟到、乱序、重复上报；并发写入下窗口状态一致。
3. **可疑案件处置流转**：创建、确认、驳回、升级，幂等、权限校验、终态冲突可区分；规则版本升级后历史案件结论可复现。

## 运行环境

- Python >= 3.14，依赖管理使用 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                       # 安装依赖（含 pytest 开发依赖）
uv run uvicorn main:app --reload   # 启动 HTTP 服务，默认 http://127.0.0.1:8000
```

启动后可访问交互式文档：`http://127.0.0.1:8000/docs`。

## 一、风险评分

### 1.1 规则与权重（`risk_engine/rules.py`）

规则按固定顺序独立判定，命中即累加权重，总分截断到 `max_score`：

| 规则 ID | 名称 | 默认权重 | 命中条件（默认阈值） |
|---|---|---|---|
| `VERY_HIGH_AMOUNT` | 巨额交易 | 50 | `amount >= 50,000` |
| `HIGH_AMOUNT` | 大额交易 | 30 | `amount >= 10,000` |
| `SUSPICIOUS_COUNTRY` | 可疑国家/地区 | 40 | `country ∈ {XX, ZZ}` |
| `NIGHT_TIME` | 夜间交易 | 15 | 交易时间小时 ∈ [0, 5) |

分级阈值：`score >= 70 → HIGH`，`score >= 40 → MEDIUM`，其余为 `LOW`。

所有阈值与权重都在 `RulesConfig` 中，可整体替换并按**版本**注册（见第四节）。
评分响应中 `hits` 数组给出每条命中规则的 ID、名称、实际权重和判定明细 `detail`，
结论中的每一分都能追溯到具体规则，保证**可解释、可复现**。

### 1.2 输入校验与拒绝原因

非法输入不参与评分，返回 `accepted=false` 与互不相同的 `reject_reason`：

| reject_reason | 触发条件 |
|---|---|
| `MISSING_FIELD` | `txn_id / account_id / device_id` 为空或非字符串 |
| `INVALID_AMOUNT` | 金额非数值、NaN/Inf、或 <= 0 |
| `INVALID_TIME` | `txn_time` 不是 datetime |
| `UNSCORABLE` | 输入合法但无任何规则命中（无法得出分数）、规则配置非法、或无规则 |

### 1.3 接口

```bash
# 单笔
curl -s -X POST http://127.0.0.1:8000/risk/score -H 'Content-Type: application/json' -d '{
  "txn_id":"t1","account_id":"a1","device_id":"d1",
  "amount":60000,"txn_time":"2026-09-23T02:00:00","country":"XX"
}'

# 批量：HTTP 207，逐笔独立结论，统计 accepted/rejected
curl -s -X POST http://127.0.0.1:8000/risk/score/batch -H 'Content-Type: application/json' -d '{
  "transactions":[
    {"txn_id":"ok","account_id":"a1","device_id":"d1","amount":60000,"txn_time":"2026-09-23T12:00:00"},
    {"txn_id":"bad","account_id":"a1","device_id":"d1","amount":-5,"txn_time":"2026-09-23T12:00:00"}
  ]
}'
```

同一笔交易从单笔或批量入口、以及重复处理，得到的 `score / verdict / hits` 完全一致。

## 二、时间窗口与可疑行为（`risk_engine/windows.py`）

按 **账户（account）** 与 **设备（device）** 两个维度，分别维护滑动窗口内的去重事件集合。
默认窗口 10 分钟，账户阈值 3、设备阈值 5，迟到容忍 1 小时（均可在 `WindowConfig` 配置）。

**状态语义：**

- **去重**：同一 `event_id` 在同一维度只计一次；重复上报返回 `status=DUPLICATE`，不增计数、不告警。
- **乱序**：事件按自带的 `event_time` 用有序列表归位，而不是按处理时刻入桶，因此先到的“新”事件与后到的“旧”事件顺序正确。
- **迟到**：事件时间早于桶内水位 `watermark` 超过 `allowed_lateness` 时返回 `TOO_LATE` 且不接纳；容忍范围内的迟到事件正常计入。
- **滑动淘汰**：水位为桶内见过的最大事件时间；每次写入后淘汰 `event_time <= watermark - window` 的旧事件。
- **阈值判定**：`suspicious = status == NEW 且 接纳后计数 >= 阈值`，边界为“恰好达到阈值”的那一个新事件。
- **并发**：桶的读、写、淘汰、判定在同一把 `RLock` 内原子完成；同事件并发上报恰好计数一次，阈值恰好告警一次，无漏报、重复告警或状态错乱。

```bash
curl -s -X POST http://127.0.0.1:8000/events -H 'Content-Type: application/json' -d '{
  "event_id":"e1","account_id":"acc1","device_id":"dev1",
  "event_time":"2026-09-23T12:00:00"
}'
# account/device 分别返回 status、count、threshold、suspicious、event_ids（判定依据）
```

## 三、案件处置流转（`risk_engine/cases.py`）

动作与角色权限矩阵：

| 动作 | 允许角色 |
|---|---|
| `CONFIRMED`（确认） | analyst, admin |
| `DISMISSED`（驳回） | analyst, admin |
| `ESCALATED`（升级） | admin |

状态：`OPEN → CONFIRMED / DISMISSED / ESCALATED`，三个结果均为终态。

错误原因可区分：

| error_code | 含义 | HTTP |
|---|---|---|
| `CASE_NOT_FOUND` | 案件不存在 | 404 |
| `PERMISSION_DENIED` | 角色无权执行该动作，状态不变 | 403 |
| `CASE_ALREADY_EXISTS` | 重复创建（原案件不受影响） | 409 |
| `ALREADY_IN_STATE` | 对已是该终态的案件重复同一动作，**幂等**（`idempotent=true`），不追加历史 | 409 |
| `CONFLICTING_STATE` | 从一个终态改成另一个不同终态，拒绝 | 409 |

并发处置同一案件时，只有一个动作生效（写入一条历史），其余得到 `CONFLICTING_STATE`；
并发提交同一动作时只有一次生效，其余得到幂等的 `ALREADY_IN_STATE`。

```bash
curl -s -X POST http://127.0.0.1:8000/cases/case-1/actions \
  -H 'Content-Type: application/json' \
  -d '{"action":"CONFIRMED","role":"analyst","reason":"确认欺诈"}'
```

## 四、规则版本与历史复现（`risk_engine/service.py`）

- `RulesRegistry` 按 `version` 注册多套 `RulesConfig`，评分时可显式指定版本，否则使用当前激活版本。
- 立案时会在案件中**固化** `rules_version` 与证据快照（分数、分级、命中规则明细）。
- 规则 / 阈值升级后：历史案件的状态、依据不变；用案件记录中的版本重新评分可得到与立案时完全一致的结论；新版本只影响新输入。
- 注册配置时会校验阈值（正数、`very_high >= high`、`0 <= medium <= high <= max`、权重非负），非法配置拒绝注册。

## 五、本地验证

```bash
uv run pytest -v -s          # 全量单测；-s 打印每例的输入、命中规则与判定依据
uv run pytest tests/test_windows.py   # 重复/乱序/迟到/并发窗口用例
uv run pytest tests/test_cases.py     # 并发处置、幂等、权限、版本复现用例
uv run pytest tests/test_scoring.py   # 阈值边界、拒绝原因、跨入口一致用例
```

测试覆盖：

- 重复、乱序、超迟到事件；窗口滑动淘汰；阈值恰好命中的边界。
- 10 线程并发重复上报（不重复计数/告警）、3 线程并发跨阈值（恰好一次告警）。
- 并发处置同一案件（不同动作互斥、相同动作幂等）、角色权限矩阵。
- 四类拒绝原因、批量部分失败隔离（HTTP 207）、单笔/批量/重复处理结论一致。
- 规则升级 v1→v2 后历史案件与重放评分结论一致。

每个用例通过 `tests/conftest.py` 的日志输出 **输入参数、命中的规则（含权重与明细）、判定依据**。

## 代码结构

```
risk_engine/
  errors.py    错误常量
  models.py    领域模型（Transaction / ScoreResult / RiskEvent / WindowState / CaseRecord）
  rules.py     RulesConfig、评分规则与权重
  scoring.py   RiskScorer：输入校验 + 单笔/批量评分
  windows.py   SlidingWindowDetector：去重、乱序/迟到、线程安全窗口
  cases.py     CaseManager：幂等、权限、终态冲突
  service.py   RiskService + RulesRegistry：统一门面与版本管理
main.py        FastAPI 入口
tests/         评分 / 窗口 / 案件 / HTTP 四组测试
```
