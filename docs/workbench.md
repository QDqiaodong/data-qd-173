# 收款核销驱动的 EV 充电准入工作台

本工作台在既有计量结算引擎（`charging.engine.apply_batch`）之外新增一层**本地收款
核销与充电准入**能力，回答三个既有系统回答不了的问题：

1. 一笔到账究竟核销了哪张应收单、还剩多少未分配？
2. 部分退单之后客户还欠多少、是否形成贷方余额？
3. 站点现在能不能让该客户开始充电？

工作台与计量结算完全解耦：

* 独立 Python API（`charging.workbench.apply_round`）与独立 CLI（`charge-access`）。
* 独立的可恢复 JSON 状态；输入的结算 `source_state` **只读、从不写回**，工作台状态
  也不会写回计量 state。
* 许可（permit）是站点的**服务准入凭据**，不是钱包余额或预付款冻结，不预占金钱。
* 不接真实银行、充电桩或远端服务；所有时间由每轮 `evaluation_at` 显式给出，不读机器
  当前时间。全部金额为整数 cents，标准库实现，无第三方依赖。

## 1. 命令与 API 面

```sh
# 首轮可省略 --state；整轮失败退出码 2，且不创建/替换任何输出文件
python -m charging.workbench.cli \
  --round fixtures/workbench/round-01.json \
  --state run/wb-state.json \
  --output-state run/wb-state.json \
  --output run/wb-result-01.json \
  [--source-state path/to/settlement-state.json]
```

安装后也提供控制台脚本 `charge-access`（见 `pyproject.toml`）。

| 参数 | 说明 |
| --- | --- |
| `--round PATH` | 本轮输入 JSON（结构见 §3）。 |
| `--state PATH` | 父工作台状态；首轮文件不存在即创建空工作台。 |
| `--output-state PATH` | 下一状态；仅在整轮全部校验通过后原子写入。 |
| `--output PATH` | 本轮结果 JSON；同样原子写入。 |
| `--source-state PATH` | 可选，计量结算 state 路径，供 round 中 `import.source_state_ref` 引用；只读，可重复给出。 |

Python 入口：

```python
from charging.workbench import apply_round, WorkbenchError

next_state, result = apply_round(parent_state, round_input)  # 首轮 parent_state=None
```

* `apply_round` 是纯函数：失败抛 `WorkbenchError`（属性 `.code` / `.message`），父状态
  与输入快照均不被修改；成功才返回完整的下一状态。
* 状态是普通 JSON 数据，落盘后跨进程读回即可恢复（`version` 当前为 1）。

## 2. 一轮的处理阶段（与 JSON 字段顺序无关）

无论字段排列如何，一轮严格按以下阶段处理；任一阶段失败，整轮拒绝、不发布任何状态：

1. **时钟校验**：`evaluation_at` 必须是带显式偏移的 UTC 瞬时，且不得早于上一轮。
2. **凭据导入**（可选 `import`，见 §4）。
3. **策略登记**（可选 `register_policies`，见 §6）。
4. **金融阶段**：`finance_operations` 按输入次序执行（见 §5）。
5. **轮末失效**：先按轮末逾期欠款把失格客户所有尚未启动的 `issued` 许可不可逆地
   `invalidated`（原因 `debt_limit_exceeded`），再把到达 `expires_at` 的未启动许可失效
   （原因 `expired`，两种原因可区分）。
6. **服务阶段**：`service_operations` 按输入次序执行（见 §7）。

因此同轮的金融变化会先驱动准入失效，再裁决 grant/begin/finish；同轮失格客户的
`begin` 不可能成功。

## 3. Round 输入结构

```json
{
  "round_id": "wb-r1",
  "evaluation_at": "2026-02-10T00:00:00+00:00",
  "import": { "...": "见 §4，可选" },
  "register_policies": [ { "...": "见 §6，可选" } ],
  "finance_operations": [ { "op": "receive|allocate|chargeback", "...": "..." } ],
  "service_operations": [ { "op": "grant|begin|finish", "...": "..." } ]
}
```

* `round_id` 为非空字符串，会原样回显在结果里。
* 金融操作放 `finance_operations`，服务操作放 `service_operations`；放错列表按
  `invalid_phase` 拒绝。
* 每个请求带不可变 `operation_id`；其身份比较针对**操作种类 + 业务载荷**，不含本轮
  `evaluation_at`，因此可在更晚的时间重试同一请求（见 §8）。

## 4. 凭据导入

`import` 适配结算侧的闭账事实，只读取以下业务字段，其余字段（尤其内部
`reflected_charge_cents`、实时 `session_charges`）一律不参与应收身份比较，也不会重算
计量：

* `source_state.closed_lines`（以 `line_id` 为键的对象）中的
  `line_id` / `session_id` / `period_id` / `charge_cents`。
* `source_state.adjustments`（数组）中的
  `adjustment_id` / `line_id` / `amount_cents`（有符号整数）。

调用方提供两张固定映射：

* `session_accounts`：为每个 `session_id` 指定固定 `account_id`。
* `period_due_at`：为每个 `period_id` 指定固定 `due_at`（带偏移 UTC 瞬时）。

### 应收口径

```
当前应收额(line) = 原始 charge_cents + Σ 关联 adjustment.amount_cents
```

* 调整无论在结算侧被放进哪个目标账期，在本工作台都作用于**所引用原 line 的应收及原
  到期日**；adjustment 上的 `period_id` 等其他字段不参与。
* 每条 `line_id` 建立一张应收单；新 adjustment 只改变其原 line 的应收，不改任何原闭账
  凭据本身。

### 快照规则（导入是“全量快照 + 只追加事实”）

* 快照必须包含先前导入的**全部** line 与 adjustment；缺失旧项按 `missing_credential`
  整轮拒绝；adjustment 引用未知 line 按 `unknown_line_reference` 拒绝。
* 既有身份金额、归属（account）、到期日不可改；同一 `line_id` / `adjustment_id` 出现
  不同内容按 `credential_conflict` 拒绝。重复快照不重复增加应收。
* 每轮可新增 line 或 adjustment，但不能撤销已接受的事实。
* 新 line 必须能解析出固定的 `account_id` 与 `due_at`，否则 `missing_mapping`；新凭据
  缺映射整轮拒绝。已有映射保持固定，冲突按 `credential_conflict` 拒绝。
* 同一 `adjustment_id` 不能出现不同内容；同一快照内 id 重复按 `invalid_import` 拒绝。

`source_state` 可内联在 round 中，也可在 CLI 上用 `import.source_state_ref` 指向磁盘上
`apply_batch` 产出的 state 文件（路径由 `--source-state` 解析，文件只读）。

## 5. 收款、核销与退单

### receive（登记收款，可带初始核销）

```json
{"op": "receive", "operation_id": "fin-1", "receipt_id": "R-001",
 "account_id": "A1", "amount_cents": 60,
 "allocations": [{"line_id": "P1:S1", "amount_cents": 60}]}
```

* 保存 `receipt_id`、`account_id`、正整数 `amount_cents`。
* `allocations` 可选，只能引用**同客户**的应收单；各项之和不得超过到账金额
  （`over_allocation`），剩余进入未分配额。
* 除同 `operation_id` 的完整重试外，第二次 receive 同一 `receipt_id` 按
  `duplicate_receipt` 拒绝。

### allocate（用收款的未分配额继续核销）

```json
{"op": "allocate", "operation_id": "fin-2", "receipt_id": "R-001",
 "allocations": [{"line_id": "P1:S2", "amount_cents": 10}]}
```

* 来源只能是所引用收款**当前的未分配额**；同一张单可由多笔收款核销。
* 不能挪用其他收款或其他客户的钱（`cross_account` / `unknown_receipt`）。

### chargeback（退单撤回）

```json
{"op": "chargeback", "operation_id": "fin-3", "receipt_id": "R-001",
 "withdrawals": [{"line_id": "P1:S1", "amount_cents": 20}],
 "unallocated_cents": 5}
```

* 必须引用原 receipt，逐单给出撤回金额，并可单独撤回未分配额。
* 可多次部分退单，但累计撤回不得超过各自**仍有效**的分配与未分配余额
  （`over_chargeback`）。撤回后的资金离开该收款，不会变回可再次 allocate 的余额。
* 退单必须保留确切原款归属；后续凭据更正不会把既有分配自动转给别单。

### 整数与“正变化”规则

所有分配、撤回及未分配撤回量均为非负整数；每次操作的实际变化总量必须为正
（零变化按 `invalid_operation` 拒绝）。

### 单据与收款视图

每张单展示：有符号 `receivable_cents`、有效 `writeoff_cents`、
`debt_cents = max(应收-核销, 0)`、`credit_cents = max(核销-应收, 0)`。负应收按原值保留；
贷方余额不自动抵其他单、不截断成零、不是可自动转移的现金。

每笔收款始终满足守恒：

```
有效核销 + 未分配 + 累计退单（撤回分配 + 撤回未分配） == 原收款金额
```

结果 JSON 的 `receipts[].conservation.balanced` 恒为 `true`（内部以断言守护）。

## 6. 客户策略

首次使用某账户（grant 或其参与准入裁决）前必须登记：

```json
{"account_id": "A1", "debt_limit_cents": 30, "grace_seconds": 86400}
```

* `debt_limit_cents`、`grace_seconds` 均为非负整数。
* 策略登记**不可变**：同内容重复登记可复用（幂等），不同内容按 `policy_conflict`
  拒绝，不支持策略修订。

逾期口径：只汇总满足 `evaluation_at` 严格晚于 due_at + grace_seconds 的各单**正欠款**。
当逾期欠款**严格大于** `debt_limit_cents` 时准入失格（恰等于限额仍合格）。

## 7. 充电许可生命周期

许可状态机：`issued → started → finished`，或 `issued → invalidated`（终态）。

### grant（签发）

```json
{"op": "grant", "operation_id": "svc-1", "permit_id": "G",
 "account_id": "A1", "station_id": "ST-ALPHA",
 "expires_at": "2026-03-01T00:00:00+00:00"}
```

* 绑定 `account_id`、`station_id`、`expires_at`；`expires_at` 必须严格晚于本轮
  `evaluation_at`（`permit_expired`）。许可不预占金钱。
* 轮末逾期欠款超限额时**拒绝签发**：这是可记录、可重试的业务结果
  （回执 `granted: false`、`denied_reason: debt_limit_exceeded`），不创建许可。

### begin（启动会话）

```json
{"op": "begin", "operation_id": "svc-2", "permit_id": "G",
 "station_id": "ST-ALPHA", "session_id": "SESSION-9001"}
```

只有同时满足下列条件才能启动，否则按对应错误码整轮拒绝：

* 许可当前仍为 `issued`（失效/过期 → `permit_not_usable`，消息携带失效原因）。
* 未过期：`evaluation_at < expires_at`。
* 站点匹配：错误站点 → `station_mismatch`。
* 当前欠款合格：失格 → `debt_not_qualified`。
* `session_id` 未被另一许可使用（`session_conflict`）；一个许可只能启动一个会话。

### finish（结束会话）

```json
{"op": "finish", "operation_id": "svc-3", "permit_id": "G",
 "session_id": "SESSION-9001"}
```

* 只有 `started` 许可可结束（`permit_not_started`），且必须指向该许可实际启动的
  `session_id`（`session_mismatch`）。`finished` 是终态，不能再次 begin。
* 已 started 的会话在客户后续失格或许可过期后仍保持可结束，轮末欠款不会反推改写过去
  已发生的启动。

### 失效语义

* 欠款失格使该客户**所有尚未启动的 issued 许可**不可逆失效；后续补款也不得复活旧许可，
  只能新签一张。
* 到达 `expires_at` 的未启动许可失效，原因记为 `expired`，与
  `debt_limit_exceeded` 可区分。
* 回执中“曾成功签发”不代表该许可当前还能启动：当前生命周期在结果 `permits[]` 中分开
  输出。

## 8. operation_id 幂等、重试与冲突

* 工作台按 `operation_id` 归档每个请求的种类、完整业务载荷与当时的业务回执。
* 完全相同请求（同种类、同载荷）的重试返回原业务回执并带 `"replayed": true`，不产生
  第二张许可、第二次核销或第二个会话，也不产生新的金额变化。
* 同一 `operation_id` 以不同载荷或不同操作种类重试 → `operation_conflict`，整轮拒绝。
  同一轮内 `operation_id` 重复也按 `operation_conflict` 拒绝。
* 身份比较不含 `evaluation_at`，旧 grant 请求可在更晚一轮重试：返回的是当时存下的
  签发回执，同时 `permits[]` 显示该许可当前可能已 `invalidated`，不会被复活。
* grant 被拒的回执同样归档；重试得到原拒绝回执，不会因后来补款而变成签发。

## 9. 状态与结果 JSON

### 工作台状态（`--output-state`）

| 字段 | 含义 |
| --- | --- |
| `version` | 状态版本（当前 1）。 |
| `last_evaluation_at` | 上一轮时钟，用于禁止时钟倒退。 |
| `lines` | 已接受的应收单身份（line/session/period、原始 charge、固定 account/due）。 |
| `adjustments` | 已接受的有符号调整身份（adjustment/line/amount）。 |
| `session_accounts` / `period_due_at` | 固定客户归属与到期映射。 |
| `policies` | 每账户不可变限额/宽限策略。 |
| `receipts` | 每笔收款的分配、累计撤回与未分配撤回（原款归属）。 |
| `permits` / `used_sessions` | 许可生命周期索引与 session→permit 占用索引。 |
| `operations` | operation_id → 种类/载荷/原回执/轮次，支撑幂等重试与恢复。 |

状态既可视为由完整不可变操作历史重建，也可直接持久化当前分配与许可索引；恢复后保留
各请求原回执、部分退单的原收款归属与不可逆失效事实。

### 轮结果（`--output`）

| 字段 | 含义 |
| --- | --- |
| `round_id` / `evaluation_at` | 本轮标识与时轴。 |
| `lines[]` | 逐单应收/有效核销/欠款/贷方余额，身份稳定可跨轮追踪。 |
| `receipts[]` | 逐收款去向、分配明细与守恒分解（`conservation`）。 |
| `accounts[]` | 每账户逾期欠款、限额、宽限、逾期单据与当前准入原因。 |
| `permits[]` | 许可当前生命周期（含失效原因/时间、绑定会话）。 |
| `invalidated_this_round[]` | 本轮失效的许可及可区分原因。 |
| `receipts_this_round[]` | 本轮请求的业务回执（重试带 `replayed: true`）。 |

## 10. 错误码参考

| code | 触发场景 |
| --- | --- |
| `invalid_round` / `invalid_state` | 轮结构错误、naive/不可解析时间、不支持的状态版本。 |
| `clock_moved_backwards` | `evaluation_at` 早于上一轮。 |
| `invalid_operation` / `invalid_phase` | 操作字段/类型/金额非法、零变化、操作放错阶段列表。 |
| `operation_conflict` | operation_id 载荷/种类冲突或轮内重复。 |
| `invalid_import` | 快照结构错误、批内 id 重复、映射值非法。 |
| `missing_credential` | 快照缺失先前已导入的 line/adjustment。 |
| `credential_conflict` | 既有凭据身份/金额/归属/到期日被修改，或固定映射被改。 |
| `missing_mapping` | 新凭据缺少 account_id 或 due_at 映射。 |
| `unknown_line_reference` / `unknown_line` / `unknown_allocation` | 引用不存在的 line 或收款下不存在的分配。 |
| `unknown_receipt` / `duplicate_receipt` / `duplicate_permit` | 收款/许可身份未知或重复登记。 |
| `over_allocation` / `over_chargeback` | 分配超未分配额、退单超仍有效余额。 |
| `cross_account` | 用某客户收款核销其他客户单据。 |
| `invalid_policy` / `policy_conflict` / `unknown_policy` | 策略字段非法、试图修订、未登记即使用账户。 |
| `permit_expired` / `permit_not_usable` / `permit_not_started` | 过期/失效许可启动、未启动即 finish。 |
| `station_mismatch` / `session_mismatch` / `session_conflict` | 站点不符、finish 会话不符、session_id 重复占用。 |
| `debt_not_qualified` | begin 当场判定账户逾期欠款超限额。 |

## 11. 边界

* 工作台只做本地核销与准入裁决；无数据库、HTTP 服务或硬件执行器。
* 状态文件为本地 JSON，多写者协调与归档保留由调用方负责。
* 所有算术均为整数 cents；不存在货币换算、税费或自动转账。
