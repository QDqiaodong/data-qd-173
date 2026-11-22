# 园区上游需量电费账单复核

园区用一只上游电表结算若干充电站的需量电费。站点进口表和上游表传回的是
**累计电量**，有时隔几个十五分钟结算时段才有一次读数。财务收到一张带峰值
时段的账单：单看全月电量对得上，还不能证明账单上的峰值发生过。本模块先
核对计量证据，再复核账单，交付一份财务可独立核查的 JSON 报告。它是与计量
结算（`charging.engine`）、收款准入（`charging.workbench`）、排程
（`charging.planning`）、启动证据核验（`charging.evidence`）、报表发布
（`charging.reporting`）并列的独立本地能力：

* Python API：`charging.demand.audit_bill(state, audit)`；
* 命令行：`python -m charging.demand.cli`（安装后亦可用 `charge-demand`）；
* **只读** `charging.engine.apply_batch` 产生的 state 与本次审计输入，
  绝不写回任何状态，既有入口、结果与状态文件保持兼容；
* 标准库实现，无第三方依赖。

## 1. 计量口径（哪些数算数）

一次核一张账单和一个园区。计量证据来自两处：

* **站点进口表**：直接读取 `apply_batch` 产出的 state，按 `state["stations"]`
  的登记找到各站进口表，取快照 `state["chains"]` 中各时刻的**有效读数**
  （`effective_cumulative_wh`）及其**头记录标识**（`head_record_id`）。
  被替代的旧读数、会话电量、闭账行和账期调整一律不参与。
* **上游表**：累计读数和记录标识由本次审计输入另给（上游表不必在 state
  中登记）。

读数曲线规则：

* 各表都须在**账期开始和结束**有有效读数；账期内可缺中间点；账期外的点
  不用于填补（缺失边界读数即 `invalid_input`）。
* 所有时刻都是带显式偏移的 ISO 时间，精确到秒，按实际瞬时归一为 UTC；
  账期内使用的读数必须落在 UTC 刻钟边界上。
* 累计 Wh 非负且不倒退（同一来源按时刻排序后单调不减）。
* 时间、站点、表源和记录身份都明确校验：站点必须在 state 中登记进口表，
  不同站点不得共用同一只进口表（不能把同一进口表当作两个站），上游表
  不得同时是站点进口表，记录标识不得重复。

**相邻读数之间的差额只约束这几个刻钟的合计**：不假设匀速充电，不沿用会话
结算的读数保持或按时长摊分。一个**可行解释**是每站每刻钟一项非负整数 Wh，
它分别满足各站逐刻钟上限和各站读数区间的差额；同刻钟各站之和作为上游用
量，又必须同时满足上游各读数区间的差额。两个来源的区间可以交叉；不能分
别挑出两套分摊当作同一份证据，亦不能只校验全期总量。

## 2. 审计输入 JSON

```json
{
  "period_id": "P-2026-03-01-peak",
  "period": {"start": "2026-03-01T00:00:00Z", "end": "2026-03-01T01:00:00Z"},
  "stations": [
    {"station_id": "A", "quarter_cap_wh": 1000},
    {"station_id": "B", "quarter_cap_wh": 1000}
  ],
  "upstream": {
    "source_id": "M-CAMPUS-FEED",
    "readings": [
      {"record_id": "up-0000", "meter_instant": "2026-03-01T00:00:00Z", "cumulative_wh": 0},
      {"record_id": "up-0045", "meter_instant": "2026-03-01T00:45:00Z", "cumulative_wh": 3000},
      {"record_id": "up-0060", "meter_instant": "2026-03-01T01:00:00Z", "cumulative_wh": 4000}
    ]
  },
  "bill": {
    "counted_quarters": ["2026-03-01T00:15:00Z", "2026-03-01T00:45:00Z"],
    "minimum_demand_wh": 1500,
    "rate_cents_per_kw": 100,
    "declared_peak_quarter_start": "2026-03-01T00:45:00Z",
    "declared_peak_wh": 1000,
    "declared_billed_wh": 1500,
    "declared_charge_cents": 600
  }
}
```

### 字段约束

| 字段 | 约束 |
| --- | --- |
| `period_id` | 非空字符串，账期标识（原样回显在报告中） |
| `period.start` / `period.end` | 带显式偏移的 ISO 时间，按 UTC 整刻钟对齐的半开账期 `[start, end)`，至少一个刻钟 |
| `stations[].station_id` | 非空、互不相同，且已在 state 中登记进口表 |
| `stations[].quarter_cap_wh` | 非负整数：该站每个刻钟的最大进口量 Wh（固定上限） |
| `upstream.source_id` | 非空字符串；不得与任一站点进口表相同 |
| `upstream.readings[]` | 非空数组；`record_id` 非空且互不重复；`meter_instant` 带显式偏移；`cumulative_wh` 非负整数；账期内读数在刻钟边界上；账期开始和结束各有读数 |
| `bill.counted_quarters` | 非空数组：本账期内计入需量的刻钟起点，互不重复 |
| `bill.minimum_demand_wh` | 非负整数：合同保底需量（按十五分钟等效 Wh 表示） |
| `bill.rate_cents_per_kw` | 非负整数费率（分/kW） |
| `bill.declared_peak_quarter_start` | 账单声明观测峰值的刻钟起点，**必须在计入集合中** |
| `bill.declared_peak_wh` / `declared_billed_wh` / `declared_charge_cents` | 非负整数：观测峰值 Wh、实际用于计价的等效 Wh、需量金额（分） |

布尔值（`true`/`false`）不会被当作整数；出现未知字段按 `invalid_input`
拒绝。本题按上游表只供这些站、无其他负荷和损耗处理。

## 3. 账单口径

* **观测峰值**是计入需量的刻钟中上游用量的最大值；声明时段必须达到该值。
  多个时段并列时，申报其中任何一个都可以。
* **计价用量**是观测峰值与保底用量中的较大者。
* 十五分钟的平均 kW 等于该刻钟 Wh 除以 250；先按精确值乘费率，最后对全
  笔需量金额做一次四舍五入到整数分（`round_half_up(billed_wh × rate /
  250)`）。
* 账单申报值**不得**反过来约束独立的计量分析：峰值范围、费用上下界只由
  计量证据决定。

## 4. 求解方法（精确，不枚举分摊）

可行解释建模为带上下界的**整数循环流**（`charging/demand/flow.py`）：

```
源 -> 站点读数区间 -> 刻钟（或合并块）-> 上游读数区间 -> 汇 -> 回边到源
```

* `源 -> 站点区间` 边固定为该站该读数区间的差额（`[delta, delta]`）；
* `站点区间 -> 刻钟` 边容量为该站逐刻钟上限；
* `刻钟 -> 上游区间` 边承载该刻钟的上游合计 `u[t]`，按需加下界/上界；
* `上游区间 -> 汇` 边固定为上游该读数区间的差额；
* 回边闭合循环。带边下界的容量网络用「固定下界 + 超级源/汇」转换后跑
  一次整数 Dinic 最大流判定可行性；整数容量保证可行流本身就是每站每
  刻钟的整数 Wh 解释。

没有被单独约束的刻钟按（站点区间 × 上游区间）**合并成块**（块容量 =
站上限 × 刻钟数；块内任意总量都能摊回各刻钟，合并不损失一般性），因此
网络规模只随读数区间数增长，与刻钟总数、电量绝对值无关。关键量都用
**二分可行性**精确求得（“`u[t] ≤ P` 仍可行”关于 `P` 单调），每次判定
只做图运算，**绝不展开成逐 Wh 的计算或枚举全部刻钟分摊**：

* **峰值最小值**：最小的 `P` 使全部计入刻钟 `u[t] ≤ P` 可行；
* **峰值最大值**：账期按所有区间边界切成 cell（同一 cell 内各刻钟归属
  完全一致、可达范围相同），按 cell 上界降序裁剪后逐 cell 求单刻钟最大
  值，不同刻钟的最大值可来自不同解释；
* **票面时段用量范围**：对该刻钟分别二分 `u[q] ≤ P` 与 `u[q] ≥ P`；
* **支持解释**：一次可行性求解（`u[q] = 申报值`，其余计入刻钟
  `u[t] ≤ 申报值`）；
* **反例**：`u[q]` 不同于申报值，或其他计入时段用量更高（即峰值最大值
  超过申报值），分别由对应的一次可行性求解或峰值最大值解释给出。

规模：31 天（2976 刻钟）× 16 站、全部刻钟计入需量、累计数百万 Wh 的
输入在数秒内完成（见 `tests/test_demand.py` 的 `ScaleTests`）。

## 5. 报告 JSON

成功时退出码 0，报告落盘（`--output`）或只打印摘要。报告公共部分：

```json
{
  "audit_version": 1,
  "period_id": "P-2026-03-01-peak",
  "period": {"start": "2026-03-01T00:00:00+00:00", "end": "2026-03-01T01:00:00+00:00", "quarters": 4},
  "metering": {
    "stations": [
      {"station_id": "A", "import_source_id": "M-ST-A", "quarter_cap_wh": 1000,
       "intervals": [
         {"start": "...T00:00:00+00:00", "end": "...T00:30:00+00:00", "quarters": 2,
          "start_record_id": "rec-a-0000", "end_record_id": "rec-a-0030",
          "start_cumulative_wh": 0, "end_cumulative_wh": 1000, "delta_wh": 1000}
       ]}
    ],
    "upstream": {"source_id": "M-CAMPUS-FEED", "intervals": ["..."]},
    "station_total_wh": 4000,
    "upstream_total_wh": 4000
  },
  "status": "feasible"
}
```

`metering` 列明所用表源、相邻读数身份（头记录标识）和差额，复核人员可
用原始输入逐项核验解释。

### 5.1 计量冲突（`status = "metering_conflict"`）

数据格式合法却不存在共同解释（例如总电量相符但某个读数区间无解）时，
报告只含公共部分与 `"status": "metering_conflict"`，**不为该账单发出
证据或计价结论**（无 `analysis` / `bill` / `verdict`）。这是正常审计
结论，CLI 退出码仍为 0。

### 5.2 独立计量分析（`status = "feasible"` 时）

```json
"analysis": {
  "counted_quarters": ["...T00:15:00+00:00", "...T00:45:00+00:00"],
  "peak_wh": {"min": 1000, "max": 2000},
  "billed_wh": {"min": 1500, "max": 2000},
  "charge_cents": {"min": 600, "max": 800},
  "min_peak_explanation": {"quarters": ["..."]},
  "max_peak_explanation": {"quarters": ["..."]}
}
```

观测峰值精确可达的最小值和最大值、相应计价用量与费用上下界，以及**两份
完整的逐站逐刻钟解释**；范围端点相同也附可复核的解释。解释按刻钟列出
各站 Wh 与该刻钟上游合计：

```json
{"start": "...T00:15:00+00:00", "station_wh": {"A": 0, "B": 500}, "upstream_wh": 500}
```

### 5.3 账单复核与裁定

```json
"bill": {
  "declared": {"peak_quarter_start": "...T00:45:00+00:00", "peak_wh": 1000,
               "billed_wh": 1500, "charge_cents": 600},
  "minimum_demand_wh": 1500,
  "rate_cents_per_kw": 100,
  "expected_billed_wh": 1500,
  "expected_charge_cents": 600,
  "arithmetic_ok": true
},
"verdict": "evidence_undetermined",
"evidence": {
  "declared_quarter_wh": {"min": 1000, "max": 1000},
  "support_explanation": {"quarters": ["..."]},
  "counterexample_explanation": {"quarters": ["..."]}
}
```

| `verdict` | 含义 |
| --- | --- |
| `billing_mismatch` | **计价不符**：票面计价用量或金额与票面峰值按合同计算的结果不同；`expected_billed_wh` / `expected_charge_cents` 给出应该填的值，独立的计量范围仍在 `analysis` 给出 |
| `evidence_unsupported` | **证据不支持**：算式正确，但没有任何可行解释能同时满足票面峰值和时段 |
| `evidence_undetermined` | **尚无法确证**：有的解释支持、有的不支持；附一份支持解释及一份反例 |
| `evidence_confirmed` | **证据确认**：所有可行解释都支持票面峰值与时段 |

反例可以是申报时段用量不同，或其他计入时段用量更高。峰值 Wh 落在总体
范围内、或者保底使金额一致，都不能代替对票面时段和 Wh 的共同核验——
支持必须是在**同一份解释**里 `u[q]` 等于申报值且全部计入时段不超过
申报值。`evidence.declared_quarter_wh` 给出申报时段用量在全部可行解释
上的精确范围（独立求解，不受票面约束）。

## 6. 错误码与失败语义

| 代码 | 触发 | CLI 退出码 |
| --- | --- | --- |
| `invalid_input` | 缺少边界读数、错误站点、非法时间、读数倒退/为负、票面峰值时段不在计入集合中、计入集合为空或越出账期、布尔冒整数、未知字段、state 结构非法等 | 2 |

输入非法时 CLI 以退出码 2 拒绝， stderr 打印 `charge-demand:
invalid_input: ...`，且**不会创建或替换**任何输出文件（已在内存中完成
全部校验与求解后才原子落盘）。计量无解不是错误，用报告中的
`metering_conflict` 状态表示（退出码 0）。

## 7. 命令与 API

```sh
python -m charging.demand.cli \
  --state run/state.json \
  --audit fixtures/demand/audit-headline.json \
  --output run/demand-report.json
```

成功时向 stdout 打印一行摘要 JSON（账期、状态、裁定、峰值与费用范围）。

```python
from charging.demand import audit_bill, DemandError

try:
    report = audit_bill(state, audit)
except DemandError as exc:
    print(exc.code, exc.message)  # invalid_input
```

`audit_bill` 是纯函数：等价输入仅改变站点、读数或计费时段的顺序（或同一
瞬时换用不同偏移写法）时，报告和所附解释逐字节一致；输入 state 不被修改。

## 8. 示例与测试

* 端到端示例：`bash examples/demand-demo.sh`（从真实 `apply_batch` state
  到 CLI，覆盖题面全部验收场景）；
* 计量批次与审计输入夹具：`fixtures/demand/`；
* 测试：`python -m unittest tests.test_demand -v`，含与独立暴力枚举器在
  数百个随机小实例上的交叉校验、题面全部验收场景、输入校验、乱序/偏移
  等价、31 天 × 16 站规模与 CLI 原子写/失败保留。
