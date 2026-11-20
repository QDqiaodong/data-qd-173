# 车队补能与电网降载邀约的联合排程

本模块在计量结算（`charging.engine`）与收款准入（`charging.workbench`）之外，
新增一层**面向未来的本地排程**能力：把车队交来的到站/离站/补能需求与电网交来
的临时降载邀约，编成一份可核查的常态充电计划、每个邀约的接受决定，以及每个
被接受邀约被激活情景下的应急计划。

* 独立 Python API（`charging.planning.plan`）与独立 CLI（`charge-plan`）。
* **只做计划**：不发控制指令、不生成计费用的实际电量；不读写计量结算或收款
  准入的任何状态文件，两套既有接口、结果与状态文件保持兼容。
* 一次规划只处理一个站点、一个 UTC 窗口；额度与计划量均为整数 Wh；标准库
  实现，无第三方依赖、不联网、不接数据库或设备。
* 电网在窗口内**最多激活一项**邀约，且最早在降载开始时才通知；签约时不知道
  是否激活。因此各邀约是**互斥激活情景**，但它们共享同一份常态计划。

## 1. 命令与 API 面

```sh
python -m charging.planning.cli \
  --fleet fixtures/planning/fleet-headline.json \
  --offers fixtures/planning/offers-headline.json \
  --output run/plan.json
```

安装后也提供控制台脚本 `charge-plan`（见 `pyproject.toml`）。

| 参数 | 说明 |
| --- | --- |
| `--fleet PATH` | 车队需求 JSON（结构见 §3）。 |
| `--offers PATH` | 电网降载邀约 JSON（结构见 §4）。 |
| `--output PATH` | 排程结果 JSON；省略则只向 stdout 打印决策摘要。仅在全部校验与求解成功后原子写入。 |
| `--search-limit N` | 联合可行性回溯的节点预算，默认 200000；超出返回 `search_limit_exceeded`。 |

Python 入口：

```python
from charging.planning import plan, PlanningError

try:
    result = plan(fleet_input, offers_input)
except PlanningError as exc:
    print(exc.code, exc.message)   # 见 §7 错误码
```

`plan` 是纯函数：相同业务内容的输入（含数组乱序）总是得到相同决策与充电分配；
失败抛 `PlanningError`（属性 `.code` / `.message`），不产生任何部分结果。

## 2. 能量口径与硬约束

* 一个站点只有**一层共享的站点额度**，不区分枪型。
* 每辆车有唯一标识、由到站/离站两个时段边界表示的可充电时段（**含到站、不含
  离站**）、每段最大充电量，以及离站前必须恰好补足的电量。
* 每辆车的计划总量**恰好等于**其需求；不得超充，也不因邀约收益取消或减量。
* 邀约激活后逐段从**正常站点额度**扣减固定正整数 Wh，降载结束后恢复；空闲
  额度也可满足扣减，报酬不按实际充电量计算。
* 整数 Wh、整数时段、整数报酬；时间一律 UTC。

### 非法输入 vs 业务不可接受（两类失败严格区分）

* **非法输入**（`invalid_input`，CLI 退出码 2）：结构错误、时间边界非法、
  整数越界、两份文件站点/窗口/时段长度不一致、车辆或邀约身份重复、降载时段
  为空或越界、报酬/扣减非正整数，以及**扣减大于覆盖时段中任一正常额度**。
* **业务不可接受**（不报错，正常出计划）：邀约本身合法，但接受后会让某辆车
  无法按时补能。该邀约标记为 `feasible_alone=false`、拒绝原因
  `infeasible_alone`，常态计划照常发布。
* **常态不可行**（`baseline_infeasible`，退出码 2）：输入合法，但即便没有任何
  激活也无法满足全部车辆。此时不发布任何缺车或超额的计划。

## 3. 车队需求文件 `--fleet`

```json
{
  "station_id": "ST-HEADLINE",
  "window_start": "2026-12-01T00:00:00Z",
  "slot_minutes": 30,
  "normal_capacity_wh": [1, 4, 1, 1],
  "vehicles": [
    {
      "vehicle_id": "A",
      "arrival_slot": 0,
      "departure_slot": 2,
      "max_charge_wh": [1, 1, 0, 0],
      "required_energy_wh": 1
    }
  ]
}
```

| 字段 | 约束 |
| --- | --- |
| `station_id` | 非空字符串；必须与邀约文件相同。 |
| `window_start` | 带显式 UTC 偏移的 ISO 时间（`Z` 或 `+00:00`）；两份文件必须相同。 |
| `slot_minutes` | 正整数，等长时段；两份文件必须相同。 |
| `normal_capacity_wh` | 非空非负整数数组，长度即时段数 `T`。 |
| `vehicles[].vehicle_id` | 非空字符串，文件内唯一。 |
| `arrival_slot` / `departure_slot` | 整数，`0 <= arrival < departure <= T`。 |
| `max_charge_wh` | 长度 `T` 的非负整数数组；在站窗口外必须为 0。 |
| `required_energy_wh` | 非负整数，且不超过在站窗口内各段上限之和。 |

## 4. 邀约文件 `--offers`

```json
{
  "station_id": "ST-HEADLINE",
  "window_start": "2026-12-01T00:00:00Z",
  "slot_minutes": 30,
  "offers": [
    {"offer_id": "O1", "start_slot": 1, "end_slot": 2,
     "reduction_wh": 4, "reward": 7}
  ]
}
```

| 字段 | 约束 |
| --- | --- |
| `offer_id` | 非空字符串，文件内唯一。 |
| `start_slot` / `end_slot` | 整数，`0 <= start < end <= T`，声明**非空连续**降载时段 `[start, end)`。 |
| `reduction_wh` | 正整数；对每个覆盖时段都必须 `<= normal_capacity_wh[t]`，否则 `invalid_input`。 |
| `reward` | 正整数报酬。 |

`offers` 允许为空数组（表示没有邀约，仍交付常态计划）。

## 5. 一份常态计划，多种可能结果

* **常态计划**在无任何激活时满足全部车辆需求（逐车总量恰等于需求、只在在站
  窗口内充电、不超车端上限、逐段站点使用量不超正常额度）。
* 对每个**被接受**的邀约交付一份应急计划。它与同一份常态计划在激活开始前的
  **每个时段、每辆车**的充电量完全相同——这些电量在通知到达时已实际安排；
  从激活开始可重新调度仍在站车辆，降载结束后的时段也允许与常态不同。
* 应急计划满足该情景的逐段**有效额度**（降载段为正常额度扣减后的值）、车辆
  在站窗口、车端上限与离站需求。
* 不能用“切换前各自独立的计划”或“只比对站点总量”来证明可兑现：校验针对的是
  逐车逐段的共同前缀。

### 组合选择规则

在所有**可共同兑现**的邀约组合中：

1. 选报酬总和最高的组合；
2. 报酬并列时，把组合内邀约标识排序成序列，取字典序较小的一组。

没有可接受邀约时，接受列表为空、合计报酬为 0，照常交付常态计划。

## 6. 结果文件结构（`--output`）

顶层包含窗口与站点、每个邀约的决定、合计报酬，以及常态与每个已接受情景的
逐车逐段充电量、逐段站点使用量与额度：

```json
{
  "plan_version": 1,
  "station_id": "ST-HEADLINE",
  "window_start": "2026-12-01T00:00:00+00:00",
  "slot_minutes": 30,
  "slot_count": 4,
  "normal_capacity_wh": [1, 4, 1, 1],
  "decision": {
    "accepted_offer_ids": ["O2"],
    "total_reward": 9,
    "offer_count": 2,
    "accepted_count": 1
  },
  "offers": [ { "...": "原样回显的邀约字段" } ],
  "offer_decisions": [
    {
      "offer_id": "O1",
      "accepted": false,
      "reward_if_accepted": 0,
      "reject_reason": "not_in_best_combination",
      "feasible_alone": true
    }
  ],
  "scenarios": [
    {
      "scenario_id": "baseline",
      "kind": "baseline",
      "offer_id": null,
      "activation_start_slot": null,
      "prefix_locked_through_slot": null,
      "vehicles": [
        {
          "vehicle_id": "A",
          "arrival_slot": 0,
          "departure_slot": 2,
          "required_energy_wh": 1,
          "planned_energy_wh": 1,
          "charge_wh_by_slot": [0, 1, 0, 0]
        }
      ],
      "slots": [
        {
          "slot": 0,
          "normal_capacity_wh": 1,
          "reduction_wh": 0,
          "effective_capacity_wh": 1,
          "station_used_wh": 1,
          "spare_capacity_wh": 0
        }
      ],
      "total_planned_wh": 3
    },
    {
      "scenario_id": "offer:O2",
      "kind": "contingency",
      "offer_id": "O2",
      "activation_start_slot": 2,
      "prefix_locked_through_slot": 2,
      "vehicles": [ { "...": "同形状，激活前与常态逐车逐段相同" } ],
      "slots": [ { "...": "降载段 reduction_wh>0、effective_capacity_wh 已扣减" } ]
    }
  ]
}
```

值班员无需了解求解内部状态即可复核：

* 每个情景 `vehicles[].planned_energy_wh == required_energy_wh`，
  且 `charge_wh_by_slot` 只在在站窗口内为正、不超车端上限；
* 每个时段 `station_used_wh <= effective_capacity_wh`，
  其中 `effective_capacity_wh = normal_capacity_wh - reduction_wh`；
* 应急情景 `t < activation_start_slot` 的逐车分配与常态完全一致
  （`prefix_locked_through_slot` 标明锁定到哪个时段）。

`offer_decisions[].reject_reason` 取值：

| 值 | 含义 |
| --- | --- |
| `infeasible_alone` | 邀约合法，但单独激活即会使车辆无法按时补能。 |
| `not_in_best_combination` | 单项可兑现，但不在报酬最高（或字典序最优）的可共同兑现组合中。 |
| `null` | 该邀约被接受。 |

## 7. 错误码与失败语义

| `code` | 含义 | CLI 退出码 |
| --- | --- | --- |
| `invalid_input` | 非法输入（见 §2）。 | 2 |
| `baseline_infeasible` | 输入合法但常态计划本身不可行。 | 2 |
| `search_limit_exceeded` | 联合回溯超过 `--search-limit` 节点预算；不返回未经验证的计划。 | 2 |

任何失败都**不会创建或替换**输出文件：全部校验与求解在内存中完成，只有成功
才原子写入；已有的结果/状态文件原样保留。stdout 仅在成功时打印一行决策摘要。

## 8. 求解方法与适用规模

* **单情景可行性**用整数最大流精确求解：源→车辆（剩余需求）→时段（车端上限
  与剩余站点额度的较小值）→汇（站点额度）。所有容量为整数，流天然给出整数
  Wh 分配，不存在四舍五入。
* **联合可兑现性**是精确回溯：枚举常态计划在“最早激活段之前”等耦合时段上的
  逐车分配，每个节点对仍被锁定的情景用前缀锁定最大流剪枝；激活较晚的邀约锁定
  更长前缀（含两次激活之间的常态段），叶节点再独立补全各情景尾部。组合搜索用
  分支限界（报酬上界 + 可行性关于集合单调的记忆化剪枝）。
* 最大流为 Dinic，回溯节点预算 `search_limit` 兜底；超出即明确报错而非近似。

适用规模（本机实测，Python 3.11、标准库、单进程）：

| 场景 | 时段 T | 车辆 | 邀约 | 典型耗时 |
| --- | --- | --- | --- | --- |
| 紧凑排班（如 24 段、8 车、4 邀约） | 24 | 8 | 4 | 毫秒级 |
| 一日 15 分钟时段、10 车、3 邀约 | 96 | 10 | 3 | 约 0.2 s |
| 一日 15 分钟时段、20 车、4 邀约、激活前缀较短 | 96 | 20 | 4 | 约 1–1.6 s |

回溯成本对“耦合前缀长度 × 同时在站车辆数 × 邀约数”敏感；当激活提前量很大、
车辆高度重叠且容量紧张时可能触及节点预算，此时 CLI 返回 `search_limit_exceeded`
并可用 `--search-limit` 提高预算。本模块面向单站点、单窗口的日级计划，不是
实时控制回路。

## 9. 确定性

* 车辆与邀约在内部按标识排序，数组输入次序不影响结果。
* 最大流边按固定顺序加入、按邻接表顺序探索；回溯按确定顺序枚举。
* 因此“相同业务内容重试”或“仅交换车队/邀约数组记录次序”得到字节一致的决策
  与充电分配（测试 `DeterminismTests` 覆盖）。

## 10. 示例与测试

* 输入示例：`fixtures/planning/fleet-*.json` 与 `fixtures/planning/offers-*.json`。
* 对应输出示例：`fixtures/planning/results/plan-*.json`。
* 演示脚本：`examples/planning-demo.sh`。
* 测试：`tests/test_planning.py`，独立复核发布的常态/应急排程，并含一个与求解
  器实现完全独立的暴力枚举交叉校验（开发期脚本，300 个随机小实例全部一致）。
