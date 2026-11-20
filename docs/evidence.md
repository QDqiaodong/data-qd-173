# 充电启动证据核验

本模块回答站点运营每周的两个问题：客服收到投诉时，哪条桩端记录可以作为某次
启动请求的确证？同一个站点的启动成功率有没有达到内部目标？它在计量结算
（`charging.engine`）、收款准入（`charging.workbench`）与未来排程
（`charging.planning`）之外，新增一层**只读日志的本地核验**能力：

* 独立 Python API（`charging.evidence.analyze`）与独立 CLI（`charge-evidence`）。
* 一次处理**同一站点的一份日志快照**：手机端/现场终端的**请求日志**与桩端的
  **结果日志**，两份日志用显式的 `observed_until`（观测截止时刻）表示口径。
* **只读**：不读写计量结算、收款准入或排程的任何状态文件；不把许可启动、
  桩端故障或计划量写进任何既有状态。三套既有接口、结果与状态文件保持兼容。
* 标准库实现，无第三方依赖、不联网、不接数据库或设备。

## 1. 命令与 API 面

```sh
python -m charging.evidence.cli \
  --requests fixtures/evidence/two-channel-requests.json \
  --results fixtures/evidence/two-channel-results.json \
  --window-start 2026-11-20T10:00:00Z \
  --window-end 2026-11-20T10:02:00Z \
  --target-bp 5000 \
  --output run/evidence.json
```

安装后也提供控制台脚本 `charge-evidence`（见 `pyproject.toml`）。

| 参数 | 说明 |
| --- | --- |
| `--requests PATH` | 请求日志 JSON（结构见 §3）。 |
| `--results PATH` | 桩端结果日志 JSON（结构见 §3）。 |
| `--window-start ISO` | 半开请求时间窗起点（含），带显式偏移。 |
| `--window-end ISO` | 半开请求时间窗终点（不含）；`observed_until` 必须不早于其 +120 秒。 |
| `--target-bp N` | 目标基点值，0 到 10000 的整数（5000 = 50%）。 |
| `--output PATH` | 结果 JSON；省略则只向 stdout 打印一行摘要。仅在全部校验与求解成功后原子写入。 |

Python 入口：

```python
from charging.evidence import analyze, EvidenceError

try:
    result = analyze(requests_doc, results_doc,
                     "2026-11-20T10:00:00Z", "2026-11-20T10:02:00Z", 5000)
except EvidenceError as exc:
    print(exc.code, exc.message)   # 见 §8 错误码
```

`analyze` 是纯函数：相同业务内容的输入（含日志行序乱序、同一瞬时的不同偏移
写法）总是得到逐字节相同的结果；失败抛 `EvidenceError`，不产生任何部分结果。

## 2. 数据口径

* **站点口径**：一次只处理一个站点；两份日志的 `station_id` 必须一致，
  `observed_until` 必须表示同一瞬时（允许不同偏移写法，按实际瞬时比较）。
* **时刻口径**：所有时刻都是带显式偏移的 ISO 时间，**精确到秒**（不允许亚秒
  部分），按实际瞬时比较；每条记录的时刻都不得晚于 `observed_until`。
* **身份口径**：请求标识在请求日志内唯一，结果标识在桩端日志内唯一；两类
  记录可以使用相同的标识字符串，不视为同一记录。空白（含全空白字符）标识、
  重复身份、非法字段、站点不一致一律拒绝（`invalid_input`）。
* **关联码口径**：`correlation_code` 可选；缺省、`null` 或空串都表示“无码”。
  关联码**不是**记录标识，可以重复出现在多条记录上。
* **观测截止与窗口**：`observed_until >= window_end + 120 秒`，保证窗内请求
  都有完整的结果等待时间；`window_start < window_end`（半开窗口非空）。

## 3. 输入文件

请求日志 `--requests`：

```json
{
  "station_id": "ST-DEMO-1",
  "observed_until": "2026-11-20T10:04:00Z",
  "requests": [
    {"request_id": "REQ-M1", "gun_id": "GUN-1",
     "requested_at": "2026-11-20T10:00:00Z", "channel": "mobile",
     "correlation_code": "K"}
  ]
}
```

桩端日志 `--results`：

```json
{
  "station_id": "ST-DEMO-1",
  "observed_until": "2026-11-20T10:04:00Z",
  "results": [
    {"result_id": "RES-S1", "gun_id": "GUN-1",
     "resolved_at": "2026-11-20T10:00:30Z", "outcome": "started"}
  ]
}
```

| 字段 | 约束 |
| --- | --- |
| `station_id` | 非空字符串；两份日志必须相同。 |
| `observed_until` | 带显式偏移、精确到秒的 ISO 时间；两份日志必须表示同一瞬时。 |
| `requests[].request_id` / `results[].result_id` | 非空（非空白）字符串，各自来源内唯一。 |
| `gun_id` | 非空（非空白）字符串。 |
| `requested_at` / `resolved_at` | 带显式偏移、精确到秒的 ISO 时间；不得晚于 `observed_until`。 |
| `channel` | `mobile`（手机端）或 `terminal`（现场终端）。 |
| `outcome` | `started`（实际启动）或 `failed`（启动故障）。 |
| `correlation_code` | 可选；字符串或 `null`；空串视同无码。 |

`requests` / `results` 允许为空数组。未列出的字段一律视为非法输入。

## 4. 关联与最优解释

* **可能关联**：同枪口，且结果时刻不早于请求时刻、落后不超过 120 秒（含边界
  0 与 120）；若两侧关联码都非空且不相同，则不能关联。
* **证据强弱**：两侧关联码相同且非空是**强证据**；仅一侧带码或两侧都无码
  仅是**普通时间证据**。
* **一次解释**：每条请求、每条结果最多配对一次，允许留下未配对项。
* **最优解释**：先最大化强证据配对数，再在此基础上最大化配对总数。不按
  时间更近、记录名称或输入顺序打破业务上的平手——平手意味着多份最优解释
  并存，模块精确地刻画它们的全体，而不是任意挑一份。

## 5. 客服证据清单（`evidence`）

覆盖快照中的全部两类记录，每条记录恰好出现一次：

| 位置 | 含义 |
| --- | --- |
| `confirmed_pairs[]` | **确证配对**：在**每份**最优解释中都出现的配对，标出两侧标识、枪口、渠道、结果类型（`outcome`）、证据类型（`evidence`: `strong`/`time`）与两侧时刻。 |
| `unmatched.requests[]` / `unmatched.results[]` | 在**任何**最优解释中都没有配对的记录。 |
| `needs_review.requests[]` / `needs_review.results[]` | 其余没有确证的记录：在部分最优解释中配对、或每份解释中都配对但配对对象不同——有待人工核查。 |

**留空口径**：某条请求没有确证配对（包括列入未配对），只表示这批证据没有
支持该关联；本模块**不生成**任何桩端故障事实，也不把未配对请求写成故障。

`matching_summary` 同时给出用于两份交付的最优强证据数（`strong_pairs`）与
配对总数（`total_pairs`），以及记录数与确证配对数。

## 6. 运营报告（`operations`）

* **窗口归属**：按请求时刻归属半开窗口 `[window_start, window_end)`；窗口外
  的请求和结果仍参与整个快照的关联（可能凭强证据占用窗内请求也能匹配的
  结果），不先裁时间窗或渠道再独立关联。
* **三组口径**：`station`（站点整体）、`mobile`、`terminal`。分母
  `requests_in_window` 为该组窗口内全部请求数；分子为其中被配对到
  `started`（实际启动）结果的请求数。
* **成功数区间**：`successes_min` / `successes_max` 为**所有最优解释**中可
  达到的最小、最大成功数；`witness_min` / `witness_max` 各附一份可核查的
  **完整配对**（该解释的全部配对，含每对的两侧标识、枪口、渠道、结果类型、
  证据类型、时刻与 `in_window` 标记）作为证据。不同组、不同端点可以来自
  不同解释；站点区间由全站点最优解释独立求解，**不是**渠道端点的相加。
* **达标判定**（整数精确比较，目标为 `target_bp` 基点值）：

| `status` | 条件（`d` = 分母） |
| --- | --- |
| `met` | `successes_min * 10000 >= target_bp * d`：最低成功率也达到目标，确认达标。 |
| `not_met` | `successes_max * 10000 < target_bp * d`：最高成功率仍低于目标，确认未达标。 |
| `undetermined` | 其余：证据不足以判定。 |
| `no_data` | `d == 0`：该组窗口内没有请求，单列为无数据，不声称达标。 |

## 7. 结果文件结构（`--output`）

```json
{
  "evidence_version": 1,
  "station_id": "ST-DEMO-1",
  "observed_until": "2026-11-20T10:04:00+00:00",
  "window": {"start": "2026-11-20T10:00:00+00:00",
             "end": "2026-11-20T10:02:00+00:00"},
  "target_bp": 5000,
  "matching_summary": {"requests": 2, "results": 2, "strong_pairs": 0,
                       "total_pairs": 2, "confirmed_pairs": 0},
  "evidence": {
    "confirmed_pairs": [],
    "unmatched": {"requests": [], "results": []},
    "needs_review": {"requests": ["..."], "results": ["..."]}
  },
  "operations": {
    "groups": {
      "station": {"requests_in_window": 2, "successes_min": 1,
                  "successes_max": 1, "status": "met",
                  "witness_min": {"strong_pairs": 0, "total_pairs": 2,
                                  "successes": 1, "pairs": ["..."]},
                  "witness_max": {"...": "同形状"}},
      "mobile": {"...": "同形状"},
      "terminal": {"...": "同形状"}
    }
  }
}
```

所有输出时刻统一规范化为 UTC ISO 表示；列表按标识字典序排列，配对按
（请求标识、结果标识）排列。

## 8. 错误码与失败语义

| `code` | 含义 | CLI 退出码 |
| --- | --- | --- |
| `invalid_input` | 非法输入：结构、未知或缺失字段、站点或观测截止不一致、空白标识、重复身份、时刻无偏移/亚秒/晚于截止、渠道或终态非法、关联码类型非法、窗口为空或倒置、观测截止早于窗口结束后 120 秒、目标基点越界或非整数。 | 2 |

任何失败都**不会创建或替换**输出文件：全部校验与求解在内存中完成，只有
成功才原子写入；已有的结果文件原样保留。stdout 仅在成功时打印一行摘要。

## 9. 求解方法与确定性

* **最优解释**：把关联候选建成二部图，按连通分量分解；每个分量用整数
  最小费用最大流求字典序最优（强证据权重 = 配对数上界 + 1，普通边为 1），
  全整数运算，没有四舍五入或近似。
* **确证/未配对判定**：对偶最优性保证“另一份最优解释 ⟺ 参考最优流叠加零
  既约费用环流”。在零既约费用残量图上求强连通分量：一对配对出现在每份
  最优解释中 ⟺ 其反向残量边不在任何零费用环流上；一条未配对记录出现在
  某份最优解释中 ⟺ 其源/汇残量边在某零费用环流上。该判定与参考解和势
  函数的选取无关，不枚举全部解释。
* **成功数区间**：第二层整数权重（第一层权重 × 上界基数 ± 成功奖励）在
  同一网络上精确求解每组的最小/最大端点及其实现解释。
* **确定性**：记录按标识字典序排列后再建图，边按固定顺序加入，最短路平局
  按节点编号裁决；因此调换日志行序后，证据清单、统计与用作证明的配对
  逐字节一致（`DeterminismTests` 覆盖）。

适用规模：分量天然按枪口与 120 秒窗口聚集，站点快照的典型分量很小；
32×32 全互容的繁忙枪口分量（测试 `BacklogTests`）在本机毫秒级完成精确
求解。本模块面向单站点日志快照的离线核验，不是实时控制回路。

## 10. 示例与测试

* 输入示例：`fixtures/evidence/two-channel-*.json`（两渠道时间歧义）、
  `fixtures/evidence/coded-*.json`（强证据优先）、
  `fixtures/evidence/backlog-*.json`（32×32 繁忙枪口积压）。
* 演示脚本：`examples/evidence-demo.sh`。
* 测试：`tests/test_evidence.py`，含题面三项关键行为、边界（恰好 120 秒、
  跨枪口、不匹配关联码、窗外强证据占用）、无数据组、非法输入、真实 CLI
  调用、行序乱序一致性，以及与实现完全独立的暴力枚举交叉校验。
