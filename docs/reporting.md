# 闭账周期充电报表的保密发布编制

本模块在不泄露受合同保护的「站点为某车队充了多少电」明细的前提下，把一个
**已关闭**计费周期的充电数据编制成可对外发布的周期报表，并给内部审核员一份
发布决定说明。它是与计量结算（`charging.engine`）、收款准入
（`charging.workbench`）、排程（`charging.planning`）、启动证据核验
（`charging.evidence`）并列的独立本地能力：

* Python API：`charging.reporting.compile_report(state, request)`；
* 命令行：`python -m charging.reporting.cli`（安装后亦可用 `charge-report`）；
* **只读** `charging.engine.apply_batch` 产生的 state，绝不写回任何状态，
  旧结算/准入/排程/证据入口与状态文件保持兼容。

## 1. 数据口径（哪些数算数）

一次只处理一个已关闭 `period_id`。本期真实用量矩阵完全来自闭账 state：

* 只取该周期 `state["closed_lines"]` 中 `billable = true` 的行，累加其整数
  `energy_wh`；未计费行（站点残量未平衡，如 `reason = station_residual`）、
  当前会话视图、以及后续的金额调整（`adjustments`）一律不参与；
* 站点从 `state["sessions"][session_id]["station_id"]` 反查；
* 车队由请求文件 `fleet_attribution` 给每个**合格计费会话**赋予唯一车队；
  缺少任一合格会话的归属即拒绝；
* 按「站点 × 车队」对整数 Wh 求和，没有合格行的格子按零处理。

聚合结果只在内存中使用，不会被写回 state。

## 2. 请求文件 JSON 结构

```json
{
  "period_id": "P-2026-01",
  "fleet_attribution": [
    {"session_id": "S-AF", "fleet_id": "F"}
  ],
  "public_caliber": {
    "stations": ["A", "B"],
    "fleets": ["F", "G"],
    "upper_bounds_wh": [
      {"station_id": "A", "fleet_id": "F", "upper_bound_wh": 10}
    ]
  },
  "historical_facts": [
    {"fact_type": "total", "station_id": null, "fleet_id": null, "value_wh": 20}
  ],
  "candidate_packages": [
    {
      "package_id": "jia",
      "value": 9,
      "requests": [
        {"fact_type": "station", "station_id": "A", "fleet_id": null, "value_wh": 0}
      ]
    }
  ],
  "protected_cells": [
    {"station_id": "A", "fleet_id": "F", "margin_wh": 2}
  ]
}
```

### 字段约束

| 字段 | 约束 |
| --- | --- |
| `period_id` | 非空字符串，必须是 state 中 `status = closed` 的周期，否则 `invalid_input` |
| `fleet_attribution[].session_id` | 非空；同一会话只能出现一次（重复赋不同车队即 `invalid_input`）；必须覆盖该周期全部 `billable` 会话 |
| `fleet_attribution[].fleet_id` | 非空字符串 |
| `public_caliber.stations` / `fleets` | 非空、内部无重复的字符串数组 |
| `upper_bounds_wh[].upper_bound_wh` | 非负整数；必须覆盖站点×车队的**每个**组合（含不可能服务的组合，给 0）；坐标必须出现在口径清单内 |
| `historical_facts[]` | 0 条或多条精确事实，见下；同一事实重复出现只算一次，若数值冲突报 `conflicting_history` |
| `candidate_packages[].package_id` | 非空、文件内唯一 |
| `candidate_packages[].value` | 正整数发布价值 |
| `candidate_packages[].requests` | 非空精确事实数组；包内不得重复请求同一事实 |
| `protected_cells[].margin_wh` | 正整数余量 `d`；保护格坐标必须在口径内、不得重复 |

布尔值（`true`/`false`）不会被当作整数；出现未知字段也按 `invalid_input` 拒绝。

### 精确事实（fact）四种形态

| `fact_type` | `station_id` | `fleet_id` | 含义 |
| --- | --- | --- | --- |
| `cell` | 必填 | 必填 | 某一格精确值 |
| `station` | 必填 | `null` | 某站点全部车队小计 |
| `fleet` | `null` | 必填 | 某车队全部站点小计 |
| `total` | `null` | `null` | 全表总计 |

* `historical_facts` 中的 `value_wh` 是已经对外公开、**不能撤下或改写**的数；
  同一事实在多份历史报表里重复出现只计一次。
* `candidate_packages[].requests` 里的 `value_wh` 仅用于定位请求的是哪个事实；
  真正发布的数值一律由本期内部汇总计算后填充（见 §5 输出）。

### 事实一致性校验

* 历史/候选事实越过相应公开上限（格值超该格上限、行小计超该行上限和等）报
  `invalid_input`；
* 本期内部汇总越过公开上限报 `invalid_input`；
* 历史精确值与本期内部汇总不一致（既有数字不能改成别的数）报
  `conflicting_history`，例如历史全表总计与本期总和不同、历史格值与本期格值不同。

## 3. 保密模型（余量的精确定义）

设保护格真实用量为 `v`、保密余量为正整数 `d`。把全部**公开事实**（公开上限、
历史精确值、本轮拟发布精确值）放在一起，必须**同时**存在两张满足所有事实、
且每个格子都落在 `[0, 自己的公开上限]` 内的整数用量表：

* 一张表中该格 `<= v - d`（低侧余量）；
* 另一张表中该格 `>= v + d`（高侧余量）。

两张表都必须满足全部公开事实；各格只能取零到自己上限之间的整数。不同保护格
可以分别用不同的可能表来验证。只要任一保护格缺任意一侧，该组事实就不能发布。

### 为什么是整数流、为什么不枚举 Wh

可能用量表天然是一张二分容量网络上的可行流：`源 -> 站点 -> 车队 -> 汇`，
站点→车队的格边容量即公开上限。四类精确事实分别对应：格边固定值、站点源边
固定、车队汇边固定、以及一条 `汇 -> 源` 回边固定为全表总计。带边下界的容量
网络用标准的「固定下界 + 超级源/汇」转换（见 `marginflow.py`），在整数容量上
跑一次 Dinic 最大流即可判定可行整数表是否存在。

余量判定用**带界改界重算**：问某格能否 `<= v-d`，就把该格边限制在 `[0, v-d]`
再跑一次可行性；问能否 `>= v+d` 就限制在 `[v+d, U]`。无论单格电量是 5 Wh 还是
数百万 Wh，每次判定都只做图运算（20 站点×12 车队规模下单次亚毫秒级），**绝不
逐格枚举 Wh 取值**。整数容量保证找到的可行流本身就是整数表。

该实现在 `tests/test_reporting.py` 中与一个独立的「枚举所有整数表」暴力求解器
在上千个随机小实例上逐格比对可行性、极值与余量结论，全部一致。

## 4. 组合搜索：整包发布、价值最高、字典序兜底

候选包是全有或全无的整体，不能只发一个包里的部分事实；同一个事实可以被多个
包同时请求。`compile_report` 在所有「使全部保护格两侧余量都成立」的包组合里：

1. 选发布价值总和最高的组合（不逐包独立审批，也不按价值从高到低贪心）；
2. 价值并列时，把各组合内包标识升序排列，取序列字典序较小者。

实现为精确分支限界（`engine._search_best`）：按价值降序、标识升序排列候选包，
用后缀价值和做上界剪枝；事实集合的保密结论按其事实键记忆化（事实数值来自内部
汇总，同键必同值），可行性关于「公开更多事实」单调。

## 5. 两份输出 JSON

### 对外报表 `--public-output`

只含周期、选中的包及其发布数值（数值来自内部汇总，不含请求方给的占位值）：

```json
{
  "report_version": 1,
  "period_id": "P-2026-01",
  "selected_packages": [
    {
      "package_id": "bing",
      "published_facts": [
        {"fact_type": "cell", "station_id": "B", "fleet_id": "F", "value_wh": 5}
      ]
    }
  ]
}
```

不含保密清单、真实明细、未发布包或任何审核信息。没有可安全发布的包时
`selected_packages` 为空数组（正常空发布）。

### 内部审核 `--review-output`

列出选中标识、总价值、保密清单，以及对**每个未选包**「若把它加入当前选中组合
会使哪些保护格失去哪一侧余量」：

```json
{
  "report_version": 1,
  "period_id": "P-2026-01",
  "selected_package_ids": ["bing", "yi"],
  "total_value": 13,
  "protected_cells": [
    {"station_id": "A", "fleet_id": "F", "margin_wh": 2}
  ],
  "rejected_packages": [
    {
      "package_id": "jia",
      "would_violate": [
        {"station_id": "A", "fleet_id": "F", "failed_side": "high"},
        {"station_id": "A", "fleet_id": "F", "failed_side": "low"}
      ],
      "reason": "secrecy_violation"
    }
  ]
}
```

`failed_side` 取值 `low`（找不到 `<= v-d` 的表）或 `high`（找不到 `>= v+d`
的表）。未选包本身仍安全、只是不在最优组合里时，`reason` 为
`not_in_best_combination` 且 `would_violate` 为空。保密清单、真实明细与未发布
数值不对外提供。

## 6. 错误码与失败语义

| 代码 | 触发 | CLI 退出码 |
| --- | --- | --- |
| `invalid_input` | 非法/未知周期、周期未关闭、缺失归属、未知坐标、越过公开上限、布尔冒整数、未知字段、非正余量等 | 2 |
| `conflicting_history` | 历史事实互相冲突，或历史精确值与本期内部汇总不一致 | 2 |
| `prior_risk` | 仅凭公开上限与历史事实（不发任何新包）就已无法满足某保护格两侧余量 | 2 |

`prior_risk` 是与「正常空发布」可区分的错误：空发布意味着在现有公开口径下余量
仍然成立、只是没有值得发的包；`prior_risk` 意味着保密余量已经被历史披露破坏。

任何失败都在内存中完成校验、不落部分结果；CLI 先在内存里跑通才原子写盘，因此
失败时已有的两份输出文件原样保留（见 `tests/test_reporting.py` 的
`test_cli_failure_preserves_existing_outputs`）。

## 7. 命令与 API

```sh
python -m charging.reporting.cli \
  --state run/state.json \
  --request fixtures/reporting/request-headline.json \
  --public-output run/report-public.json \
  --review-output run/report-review.json
```

成功时退出码 0，并向 stdout 打印一行摘要 JSON（周期、选中标识、包数、总价值）。

```python
from charging.reporting import compile_report, ReportingError

try:
    public_report, internal_review = compile_report(state, request)
except ReportingError as exc:
    print(exc.code, exc.message)  # invalid_input / conflicting_history / prior_risk
```

`compile_report` 是纯函数：相同业务内容的输入（仅数组记录顺序不同）得到字节一致
的两份输出（测试 `test_shuffled_records_give_identical_outputs` 覆盖）。

## 8. 求解方法与适用规模

* 单事实可行性/余量：带下界整数循环流 + 一次 Dinic 最大流，节点数
  `S + F + 4`，边数 `S*F + S + F + 1`；
* 组合搜索：至多 16 个候选包时为 2^16 子集的分支限界，配后缀价值剪枝与事实集
  记忆化；支持至少 20 个站点、12 个车队、16 个候选包，周期电量达数百万 Wh；
* 标准库实现，无第三方依赖；边按固定顺序加入、BFS/DFS 按邻接表顺序探索，
  结果确定且与记录次序无关。

## 9. 示例与测试

* 端到端示例：`bash examples/reporting-demo.sh`（从 `apply_batch` 闭账结果跑到
  两份 CLI 输出，覆盖主场景、历史小计、同价裁决、空发布与 `prior_risk`）；
* 计量批次/请求夹具：`fixtures/reporting/`；
* 测试：`python -m unittest tests.test_reporting -v`，含整数流对暴力枚举的上千
  随机实例交叉校验、题面全部验收场景、CLI 原子写/失败保留、乱序确定性。
