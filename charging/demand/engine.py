"""园区上游需量电费账单复核（纯函数、只读、不写回结算状态）。

完整契约见 ``docs/demand.md``。一次核对**一张账单**和**一个园区**：

* 直接读取 :func:`charging.engine.apply_batch` 产出的 state：按站点登记的
  进口表，取快照 ``chains`` 中各时刻的有效读数及其头记录标识；被替代的旧
  读数、会话电量、闭账行和账期调整都不参与。上游表的累计读数与记录标识由
  本次审计输入另给。
* 相邻读数之间的差额只约束这几个刻钟的合计：不假设匀速充电，不沿用会话
  结算的读数保持或按时长摊分。一个可行解释是每站每刻钟一项非负整数 Wh，
  同时满足各站逐刻钟上限、各站读数区间差额与上游各读数区间差额；两个来源
  的区间可以交叉，必须同一份解释同时满足（建模为带上下界整数循环流，见
  :mod:`charging.demand.flow`），不能只校验全期总量。
* 账单口径：观测峰值是计入需量的刻钟中上游用量的最大值，声明时段必须达到
  该值；计价用量是观测峰值与合同保底的较大者；十五分钟平均 kW = Wh / 250，
  按精确值乘费率后对全笔金额做一次四舍五入到整数分。账单申报值不反过来
  约束独立的计量分析。
* 裁定：计价不符 / 证据不支持 / 尚无法确证 / 证据确认；计量证据本身格式
  合法却不存在共同解释时，报告独立的计量冲突，不为该账单发出证据或计价
  结论。输入非法（缺边界读数、错误站点、非法时间、票面峰值时段不在计入
  集合中等）抛 :class:`DemandError`，CLI 以退出码 2 拒绝并保留原输出。

本模块与计量结算、收款准入、排程、证据核验、报表发布完全解耦：只读
state 与审计输入，不写回任何状态。
"""

from datetime import datetime, timedelta, timezone

from ..core import _round_half_up
from .flow import Circulation

AUDIT_VERSION = 1

QUARTER_SECONDS = 900

# 裁定结论
VERDICT_BILLING_MISMATCH = "billing_mismatch"        # 计价不符
VERDICT_UNSUPPORTED = "evidence_unsupported"         # 证据不支持
VERDICT_UNDETERMINED = "evidence_undetermined"       # 尚无法确证
VERDICT_CONFIRMED = "evidence_confirmed"             # 证据确认

# 计量证据状态
STATUS_FEASIBLE = "feasible"                   # 存在可行解释
STATUS_CONFLICT = "metering_conflict"          # 计量冲突：不存在共同解释


class DemandError(ValueError):
    """审计输入未通过校验。

    ``code`` 为稳定机器可读错误码（CLI 退出码 2 时打印），``message`` 为
    人类可读说明。本模块只有一类失败：``invalid_input``（结构、字段、
    站点口径、时刻、读数曲线、账单字段等非法输入）。计量无解不是错误，
    用报告中的 ``metering_conflict`` 状态表示。
    """

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _require(cond, message):
    if not cond:
        raise DemandError("invalid_input", message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value, what):
    _require(isinstance(value, dict), f"{what} 必须是 JSON 对象")


def _require_str(value, what):
    _require(isinstance(value, str) and value.strip() != "",
             f"{what} 必须是非空（非空白）字符串")


def _require_nonneg_int(value, what):
    _require(_is_int(value) and value >= 0, f"{what} 必须是非负整数")


def _check_known_keys(obj, allowed, what):
    extra = sorted(set(obj) - set(allowed))
    _require(not extra, f"{what} 含未知字段: {extra}")


def _require_keys(obj, required, what):
    for key in required:
        _require(key in obj, f"{what} 缺少字段 {key!r}")


def _parse_instant(value, what):
    """要求带显式偏移、精确到秒的 ISO 时间；按实际瞬时归一为 UTC。"""
    _require(isinstance(value, str) and value != "",
             f"{what} 必须是带显式偏移的 ISO 时间字符串")
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DemandError("invalid_input", f"{what} 无法解析: {value!r}") from exc
    _require(d.tzinfo is not None and d.utcoffset() is not None,
             f"{what} 必须带显式偏移: {value!r}")
    _require(d.microsecond == 0, f"{what} 必须精确到秒（不允许亚秒部分）: {value!r}")
    return d.astimezone(timezone.utc)


def _iso(dt):
    """统一规范化为 UTC ISO 表示，保证输出与输入写法无关。"""
    return dt.astimezone(timezone.utc).isoformat()


def _require_quarter_aligned(dt, what):
    seconds = dt.hour * 3600 + dt.minute * 60 + dt.second
    _require(seconds % QUARTER_SECONDS == 0,
             f"{what} 必须落在 UTC 刻钟边界上: {_iso(dt)}")


# ---------------------------------------------------------------------------
# 1) 输入解析与校验
# ---------------------------------------------------------------------------

def _quarter_index(dt, start):
    return int((dt - start).total_seconds()) // QUARTER_SECONDS


def _parse_reading_list(raw, what):
    """解析一组 ``{record_id, meter_instant, cumulative_wh}`` 读数记录。"""
    _require(isinstance(raw, list) and raw, f"{what} 必须是非空数组")
    readings = []
    seen_ids = set()
    for idx, item in enumerate(raw):
        w = f"{what}[{idx}]"
        _require_object(item, w)
        _check_known_keys(item, ("record_id", "meter_instant", "cumulative_wh"), w)
        _require_keys(item, ("record_id", "meter_instant", "cumulative_wh"), w)
        rec_id = item["record_id"]
        _require_str(rec_id, f"{w}.record_id")
        _require(rec_id not in seen_ids, f"{what} 内 record_id 重复: {rec_id!r}")
        seen_ids.add(rec_id)
        instant = _parse_instant(item["meter_instant"], f"{w}.meter_instant")
        cum = item["cumulative_wh"]
        _require_nonneg_int(cum, f"{w}.cumulative_wh")
        readings.append({"instant": instant, "cumulative_wh": cum, "record_id": rec_id})
    return readings


def _readings_to_intervals(readings, start, end, total_quarters, what):
    """把一只表的读数序列规范化为账期内的读数区间列表。

    账期外的点不用于填补；账期内的点必须在刻钟边界上；累计 Wh 非负且
    不倒退；账期开始和结束都必须有有效读数。
    """
    seq = sorted(readings, key=lambda r: r["instant"])
    for a, b in zip(seq, seq[1:]):
        _require(b["instant"] > a["instant"],
                 f"{what} 在同一时刻 {_iso(a['instant'])} 存在多条记录")
        _require(b["cumulative_wh"] >= a["cumulative_wh"],
                 f"{what} 的累计读数在 {_iso(b['instant'])} 倒退 "
                 f"({a['cumulative_wh']} -> {b['cumulative_wh']})")
    in_period = [r for r in seq if start <= r["instant"] <= end]
    for r in in_period:
        _require_quarter_aligned(r["instant"], f"{what} 的读数时刻")
    _require(in_period and in_period[0]["instant"] == start,
             f"{what} 缺少账期开始 {_iso(start)} 的有效读数（账期外的点不用于填补）")
    _require(in_period[-1]["instant"] == end,
             f"{what} 缺少账期结束 {_iso(end)} 的有效读数（账期外的点不用于填补）")
    intervals = []
    for a, b in zip(in_period, in_period[1:]):
        qs = _quarter_index(a["instant"], start)
        qe = _quarter_index(b["instant"], start)
        intervals.append({
            "qs": qs, "qe": qe,
            "delta": b["cumulative_wh"] - a["cumulative_wh"],
            "start_record_id": a["record_id"], "end_record_id": b["record_id"],
            "start_cumulative_wh": a["cumulative_wh"],
            "end_cumulative_wh": b["cumulative_wh"],
        })
    assert intervals and intervals[0]["qs"] == 0 and intervals[-1]["qe"] == total_quarters
    return intervals


def _state_import_readings(state, needed_sources):
    """从 apply_batch state 的 ``chains`` 快照取各进口表的有效读数。

    返回 ``{source_id: [{instant, cumulative_wh, record_id}]}``；只取头记录
    （有效读数），被替代的旧记录不参与。
    """
    _require_object(state, "state")
    _require(state.get("version") == 1, "state.version 不受支持（仅支持 version=1）")
    stations = state.get("stations")
    chains = state.get("chains")
    _require(isinstance(stations, dict), "state 缺少 stations 结构")
    _require(isinstance(chains, list), "state 缺少 chains 结构")
    found = {src: [] for src in needed_sources}
    for idx, entry in enumerate(chains):
        w = f"state.chains[{idx}]"
        _require_object(entry, w)
        source_id = entry.get("source_id")
        if source_id not in found:
            continue  # 会话表等其它来源不参与
        _check_known_keys(entry, ("source_id", "meter_instant", "head_record_id",
                                  "effective_cumulative_wh", "chain"), w)
        _require_keys(entry, ("meter_instant", "head_record_id",
                              "effective_cumulative_wh"), w)
        rec_id = entry["head_record_id"]
        _require_str(rec_id, f"{w}.head_record_id")
        instant = _parse_instant(entry["meter_instant"], f"{w}.meter_instant")
        cum = entry["effective_cumulative_wh"]
        _require_nonneg_int(cum, f"{w}.effective_cumulative_wh")
        found[source_id].append(
            {"instant": instant, "cumulative_wh": cum, "record_id": rec_id})
    return found


_AUDIT_KEYS = ("period_id", "period", "stations", "upstream", "bill")
_STATION_KEYS = ("station_id", "quarter_cap_wh")
_UPSTREAM_KEYS = ("source_id", "readings")
_BILL_KEYS = ("counted_quarters", "minimum_demand_wh", "rate_cents_per_kw",
              "declared_peak_quarter_start", "declared_peak_wh",
              "declared_billed_wh", "declared_charge_cents")


def _parse_audit(state, audit):
    """校验审计输入与 state，构造规范化模型（canonical model）。"""
    _require_object(audit, "audit")
    _check_known_keys(audit, _AUDIT_KEYS, "audit")
    for key in _AUDIT_KEYS:
        _require(key in audit, f"audit 缺少字段 {key!r}")

    period_id = audit["period_id"]
    _require_str(period_id, "period_id")

    raw_period = audit["period"]
    _require_object(raw_period, "period")
    _check_known_keys(raw_period, ("start", "end"), "period")
    for key in ("start", "end"):
        _require(key in raw_period, f"period 缺少字段 {key!r}")
    start = _parse_instant(raw_period["start"], "period.start")
    end = _parse_instant(raw_period["end"], "period.end")
    _require(start < end, f"账期开始必须早于结束: {_iso(start)} !< {_iso(end)}")
    _require_quarter_aligned(start, "period.start")
    _require_quarter_aligned(end, "period.end")
    total_quarters = _quarter_index(end, start)
    _require(total_quarters >= 1, "账期必须至少包含一个刻钟")

    # ---- 站点清单：身份、上限、与 state 的登记关系 ----
    raw_stations = audit["stations"]
    _require(isinstance(raw_stations, list) and raw_stations,
             "stations 必须是非空数组")
    state_stations = state.get("stations") if isinstance(state, dict) else None
    _require(isinstance(state_stations, dict), "state 缺少 stations 结构")
    seen_stations = set()
    station_specs = []
    for idx, item in enumerate(raw_stations):
        w = f"stations[{idx}]"
        _require_object(item, w)
        _check_known_keys(item, _STATION_KEYS, w)
        _require_keys(item, _STATION_KEYS, w)
        sid = item["station_id"]
        _require_str(sid, f"{w}.station_id")
        _require(sid not in seen_stations, f"站点重复: {sid!r}")
        seen_stations.add(sid)
        cap = item["quarter_cap_wh"]
        _require_nonneg_int(cap, f"{w}.quarter_cap_wh")
        reg = state_stations.get(sid)
        _require(isinstance(reg, dict) and isinstance(reg.get("import_source_id"), str)
                 and reg["import_source_id"],
                 f"站点 {sid!r} 未在 state 中登记进口表（错误站点）")
        station_specs.append({"station_id": sid, "cap": cap,
                              "import_source_id": reg["import_source_id"]})
    station_specs.sort(key=lambda s: s["station_id"])
    sources = [s["import_source_id"] for s in station_specs]
    _require(len(set(sources)) == len(sources),
             "不同站点不得共用同一只进口表（不能把同一进口表当作两个站）")

    # ---- 上游表读数（本次输入另给） ----
    raw_upstream = audit["upstream"]
    _require_object(raw_upstream, "upstream")
    _check_known_keys(raw_upstream, _UPSTREAM_KEYS, "upstream")
    for key in _UPSTREAM_KEYS:
        _require(key in raw_upstream, f"upstream 缺少字段 {key!r}")
    upstream_source = raw_upstream["source_id"]
    _require_str(upstream_source, "upstream.source_id")
    _require(upstream_source not in set(sources),
             f"上游表 {upstream_source!r} 不得同时是站点进口表")
    upstream_readings = _parse_reading_list(raw_upstream["readings"],
                                            "upstream.readings")

    # ---- 账单 ----
    raw_bill = audit["bill"]
    _require_object(raw_bill, "bill")
    _check_known_keys(raw_bill, _BILL_KEYS, "bill")
    for key in _BILL_KEYS:
        _require(key in raw_bill, f"bill 缺少字段 {key!r}")
    raw_counted = raw_bill["counted_quarters"]
    _require(isinstance(raw_counted, list) and raw_counted,
             "bill.counted_quarters 必须是非空数组")
    counted = set()
    for idx, value in enumerate(raw_counted):
        instant = _parse_instant(value, f"bill.counted_quarters[{idx}]")
        _require_quarter_aligned(instant, f"bill.counted_quarters[{idx}]")
        _require(start <= instant < end,
                 f"计入需量的刻钟 {_iso(instant)} 不在账期 [{_iso(start)}, {_iso(end)}) 内")
        counted.add(_quarter_index(instant, start))
    _require(len(counted) == len(raw_counted),
             "bill.counted_quarters 内存在重复刻钟")
    minimum_demand = raw_bill["minimum_demand_wh"]
    _require_nonneg_int(minimum_demand, "bill.minimum_demand_wh")
    rate = raw_bill["rate_cents_per_kw"]
    _require_nonneg_int(rate, "bill.rate_cents_per_kw")
    peak_start = _parse_instant(raw_bill["declared_peak_quarter_start"],
                                "bill.declared_peak_quarter_start")
    _require_quarter_aligned(peak_start, "bill.declared_peak_quarter_start")
    _require(start <= peak_start < end,
             f"票面峰值时段 {_iso(peak_start)} 不在账期内")
    peak_quarter = _quarter_index(peak_start, start)
    _require(peak_quarter in counted,
             f"票面峰值时段 {_iso(peak_start)} 不在计入需量的刻钟集合中")
    declared_peak = raw_bill["declared_peak_wh"]
    _require_nonneg_int(declared_peak, "bill.declared_peak_wh")
    declared_billed = raw_bill["declared_billed_wh"]
    _require_nonneg_int(declared_billed, "bill.declared_billed_wh")
    declared_charge = raw_bill["declared_charge_cents"]
    _require_nonneg_int(declared_charge, "bill.declared_charge_cents")

    # ---- 计量曲线：站点进口表取 state 快照，上游表取本次输入 ----
    state_readings = _state_import_readings(state, set(sources))
    stations = []
    for spec in station_specs:
        src = spec["import_source_id"]
        readings = state_readings[src]
        _require(readings, f"站点 {spec['station_id']!r} 的进口表 {src!r} "
                           "在 state 快照中没有任何有效读数")
        intervals = _readings_to_intervals(
            readings, start, end, total_quarters,
            f"站点 {spec['station_id']!r} 的进口表 {src!r}")
        stations.append({
            "station_id": spec["station_id"],
            "cap": spec["cap"],
            "import_source_id": src,
            "intervals": intervals,
        })
    upstream_intervals = _readings_to_intervals(
        upstream_readings, start, end, total_quarters,
        f"上游表 {upstream_source!r}")

    return {
        "period_id": period_id,
        "start": start,
        "end": end,
        "T": total_quarters,
        "stations": stations,
        "upstream": {"source_id": upstream_source, "intervals": upstream_intervals},
        "counted": sorted(counted),
        "bill": {
            "peak_quarter": peak_quarter,
            "declared_peak_wh": declared_peak,
            "declared_billed_wh": declared_billed,
            "declared_charge_cents": declared_charge,
            "minimum_demand_wh": minimum_demand,
            "rate_cents_per_kw": rate,
        },
    }


# ---------------------------------------------------------------------------
# 2) 可行解释：带上下界整数循环流
# ---------------------------------------------------------------------------

def _station_interval_of_intervals(intervals, T):
    """每个刻钟所属的站点读数区间下标（区间是 ``[0, T)`` 的划分）。"""
    out = [0] * T
    for i, iv in enumerate(intervals):
        for t in range(iv["qs"], iv["qe"]):
            out[t] = i
    return out


class _AllocationModel:
    """把计量证据编译成可重复求解的循环流模板。

    变量是每站每刻钟的非负整数 Wh。没有被单独约束的刻钟按
    （站点区间 × 上游区间）合并成块：块内刻钟可任意分配，合并不损失
    一般性（块容量 = 站上限 × 刻钟数，任何块总量都能 greedy 摊回各刻钟）。
    """

    def __init__(self, model):
        self.model = model
        self.T = model["T"]
        self.stations = model["stations"]
        self.up_intervals = model["upstream"]["intervals"]
        self.total_wh = sum(iv["delta"] for iv in self.up_intervals)
        # 每个刻钟所属的上游区间
        self.up_of = [0] * self.T
        for j, iv in enumerate(self.up_intervals):
            for t in range(iv["qs"], iv["qe"]):
                self.up_of[t] = j
        # 每站每个刻钟所属的站点区间
        self.st_interval_of = [
            _station_interval_of_intervals(st["intervals"], self.T)
            for st in self.stations]
        # 块：station_idx -> interval_idx -> {up_j: [quarters]}
        self.blocks = []
        for s_idx, st in enumerate(self.stations):
            per_interval = []
            for i, iv in enumerate(st["intervals"]):
                by_up = {}
                for t in range(iv["qs"], iv["qe"]):
                    by_up.setdefault(self.up_of[t], []).append(t)
                per_interval.append(by_up)
            self.blocks.append(per_interval)

    def solve(self, special=None):
        """求解一组可行解释；``special`` 给出单独约束的刻钟 ``{t: (lo, hi)}``
        （对该刻钟上游合计 ``u[t]`` 的上下界）。无可行解释返回 ``None``，
        否则返回 ``(x, u)``：``x[s][t]`` 为各站各刻钟 Wh，``u[t]`` 为上游合计。"""
        special = special or {}
        stations = self.stations
        n_up = len(self.up_intervals)
        # 节点编号：0=SRC, 1=SNK, 站点区间 2.., 上游区间, 特殊刻钟, 块
        si_node = {}
        node = 2
        for s_idx, st in enumerate(stations):
            for i in range(len(st["intervals"])):
                si_node[(s_idx, i)] = node
                node += 1
        up_node = {}
        for j in range(n_up):
            up_node[j] = node
            node += 1
        q_node = {}
        for t in sorted(special):
            q_node[t] = node
            node += 1
        block_node = {}
        block_quarters = {}
        for s_idx, st in enumerate(stations):
            for i, by_up in enumerate(self.blocks[s_idx]):
                for j in sorted(by_up):
                    quarters = [t for t in by_up[j] if t not in special]
                    if quarters:
                        block_node[(s_idx, i, j)] = node
                        block_quarters[(s_idx, i, j)] = quarters
                        node += 1
        circ = Circulation(node)
        inf = self.total_wh
        edge_refs = []  # (kind, key, edge_id)
        for s_idx, st in enumerate(stations):
            for i, iv in enumerate(st["intervals"]):
                edge_refs.append(("si", (s_idx, i),
                                  circ.add_edge(0, si_node[(s_idx, i)],
                                                iv["delta"], iv["delta"])))
        for t in sorted(special):
            for s_idx, st in enumerate(stations):
                i = self.st_interval_of[s_idx][t]
                edge_refs.append(("si_q", (s_idx, t),
                                  circ.add_edge(si_node[(s_idx, i)], q_node[t],
                                                0, st["cap"])))
        for s_idx, st in enumerate(stations):
            cap = st["cap"]
            for i in range(len(st["intervals"])):
                for j in sorted(self.blocks[s_idx][i]):
                    key = (s_idx, i, j)
                    if key in block_node:
                        edge_refs.append(("si_b", key,
                                          circ.add_edge(si_node[(s_idx, i)],
                                                        block_node[key], 0,
                                                        cap * len(block_quarters[key]))))
        for t in sorted(special):
            lo, hi = special[t]
            edge_refs.append(("q_up", t,
                              circ.add_edge(q_node[t], up_node[self.up_of[t]], lo, hi)))
        for key in sorted(block_node):
            edge_refs.append(("b_up", key,
                              circ.add_edge(block_node[key], up_node[key[2]], 0, inf)))
        for j, iv in enumerate(self.up_intervals):
            edge_refs.append(("up", j,
                              circ.add_edge(up_node[j], 1, iv["delta"], iv["delta"])))
        circ.add_edge(1, 0, 0, inf)  # 回边：闭合循环流

        flows = circ.solve()
        if flows is None:
            return None
        edge_flow = {}
        for kind, key, eid in edge_refs:
            edge_flow[(kind, key)] = flows[eid]

        # 还原完整解释：特殊刻钟直接取边流量，块内 greedy 摊分
        x = [[0] * self.T for _ in stations]
        for (kind, key), f in edge_flow.items():
            if kind == "si_q":
                s_idx, t = key
                x[s_idx][t] = f
            elif kind == "si_b":
                s_idx, i, j = key
                cap = stations[s_idx]["cap"]
                rest = f
                for t in block_quarters[key]:
                    give = min(cap, rest)
                    x[s_idx][t] = give
                    rest -= give
                assert rest == 0
        u = [sum(x[s_idx][t] for s_idx in range(len(stations)))
             for t in range(self.T)]
        return x, u


def _cells(model):
    """把账期切成极大刻钟段（cell）：同一段内各刻钟在所有站点区间与上游
    区间上的归属完全一致，因此它们的可达成上游用量范围相同。"""
    boundaries = {0, model["T"]}
    for st in model["stations"]:
        for iv in st["intervals"]:
            boundaries.add(iv["qs"])
            boundaries.add(iv["qe"])
    for iv in model["upstream"]["intervals"]:
        boundaries.add(iv["qs"])
        boundaries.add(iv["qe"])
    points = sorted(boundaries)
    return list(zip(points, points[1:]))


# ---------------------------------------------------------------------------
# 3) 计量分析：可行性、峰值范围、逐刻钟解释
# ---------------------------------------------------------------------------

def _min_peak(model, alloc):
    """观测峰值（计入刻钟上游合计的最大值）精确可达的最小值及其实现解释。"""
    counted = model["counted"]
    sum_caps = sum(st["cap"] for st in model["stations"])
    max_up = max(iv["delta"] for iv in model["upstream"]["intervals"])
    lo, hi = 0, min(sum_caps, max_up)
    while lo < hi:
        mid = (lo + hi) // 2
        if alloc.solve({t: (0, mid) for t in counted}) is not None:
            hi = mid
        else:
            lo = mid + 1
    result = alloc.solve({t: (0, lo) for t in counted})
    assert result is not None
    return lo, result


def _max_quarter_reach(alloc, t, lo_hint, hi):
    """单刻钟上游合计 ``u[t]`` 精确可达的最大值（二分可行性，不枚举 Wh）。"""
    lo = lo_hint
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if alloc.solve({t: (mid, alloc.total_wh)}) is not None:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _max_peak(model, alloc):
    """观测峰值精确可达的最大值及其实现解释。

    同一 cell 内各刻钟的可达成范围相同，按 cell 上界降序裁剪后逐 cell
    精确求单刻钟最大值；不同刻钟的最大值可以来自不同解释。
    """
    counted = set(model["counted"])
    sum_caps = sum(st["cap"] for st in model["stations"])
    candidates = []
    for a, b in _cells(model):
        t = next((t for t in range(a, b) if t in counted), None)
        if t is None:
            continue
        local = 0
        for s_idx, st in enumerate(model["stations"]):
            i = alloc.st_interval_of[s_idx][t]
            local += min(st["cap"], st["intervals"][i]["delta"])
        ub = min(local, model["upstream"]["intervals"][alloc.up_of[t]]["delta"])
        candidates.append((ub, t))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    best = -1
    best_t = None
    for ub, t in candidates:
        if ub <= best:
            continue  # 该 cell 不可能刷新当前最大值
        if alloc.solve({t: (best + 1, alloc.total_wh)}) is None:
            continue  # 连 best+1 都达不到
        reach = _max_quarter_reach(alloc, t, best + 1, ub)
        if reach > best:
            best, best_t = reach, t
    assert best_t is not None
    result = alloc.solve({best_t: (best, alloc.total_wh)})
    assert result is not None
    return best, result


def _charge_cents(billed_wh, rate):
    """全笔需量金额：精确 kW（Wh/250）乘费率后一次四舍五入到整数分。"""
    return _round_half_up(billed_wh * rate, 250)


def _billed_wh(peak_wh, minimum_demand_wh):
    return max(peak_wh, minimum_demand_wh)


# ---------------------------------------------------------------------------
# 4) 报告装配
# ---------------------------------------------------------------------------

def _interval_json(model, iv):
    start = model["start"]
    return {
        "start": _iso(start + timedelta(seconds=iv["qs"] * QUARTER_SECONDS)),
        "end": _iso(start + timedelta(seconds=iv["qe"] * QUARTER_SECONDS)),
        "quarters": iv["qe"] - iv["qs"],
        "start_record_id": iv["start_record_id"],
        "end_record_id": iv["end_record_id"],
        "start_cumulative_wh": iv["start_cumulative_wh"],
        "end_cumulative_wh": iv["end_cumulative_wh"],
        "delta_wh": iv["delta"],
    }


def _metering_json(model):
    stations = []
    for st in model["stations"]:
        stations.append({
            "station_id": st["station_id"],
            "import_source_id": st["import_source_id"],
            "quarter_cap_wh": st["cap"],
            "intervals": [_interval_json(model, iv) for iv in st["intervals"]],
        })
    return {
        "stations": stations,
        "upstream": {
            "source_id": model["upstream"]["source_id"],
            "intervals": [_interval_json(model, iv)
                          for iv in model["upstream"]["intervals"]],
        },
        "station_total_wh": sum(iv["delta"] for st in model["stations"]
                                for iv in st["intervals"]),
        "upstream_total_wh": sum(iv["delta"] for iv in model["upstream"]["intervals"]),
    }


def _explanation_json(model, x, u):
    """逐站逐刻钟的完整解释：站点、时段、各站 Wh 与该刻钟上游合计。"""
    start = model["start"]
    quarters = []
    for t in range(model["T"]):
        quarters.append({
            "start": _iso(start + timedelta(seconds=t * QUARTER_SECONDS)),
            "station_wh": {st["station_id"]: x[s_idx][t]
                           for s_idx, st in enumerate(model["stations"])},
            "upstream_wh": u[t],
        })
    return {"quarters": quarters}


def _base_report(model):
    return {
        "audit_version": AUDIT_VERSION,
        "period_id": model["period_id"],
        "period": {
            "start": _iso(model["start"]),
            "end": _iso(model["end"]),
            "quarters": model["T"],
        },
        "metering": _metering_json(model),
    }


def _audit_model(model):
    """在规范化模型上执行完整复核，返回可 JSON 序列化的报告。"""
    alloc = _AllocationModel(model)
    report = _base_report(model)

    station_total = report["metering"]["station_total_wh"]
    upstream_total = report["metering"]["upstream_total_wh"]
    feasible = (station_total == upstream_total
                and alloc.solve() is not None)
    if not feasible:
        # 计量冲突：数据格式合法却不存在共同解释；不发证据或计价结论。
        report["status"] = STATUS_CONFLICT
        return report

    report["status"] = STATUS_FEASIBLE
    bill = model["bill"]
    rate = bill["rate_cents_per_kw"]
    minimum = bill["minimum_demand_wh"]

    # ---- 独立的计量分析：峰值范围、费用上下界、两份完整解释 ----
    peak_min, (x_min, u_min) = _min_peak(model, alloc)
    peak_max, (x_max, u_max) = _max_peak(model, alloc)
    counted_iso = [_iso(model["start"] + timedelta(seconds=t * QUARTER_SECONDS))
                   for t in model["counted"]]
    report["analysis"] = {
        "counted_quarters": counted_iso,
        "peak_wh": {"min": peak_min, "max": peak_max},
        "billed_wh": {"min": _billed_wh(peak_min, minimum),
                      "max": _billed_wh(peak_max, minimum)},
        "charge_cents": {"min": _charge_cents(_billed_wh(peak_min, minimum), rate),
                         "max": _charge_cents(_billed_wh(peak_max, minimum), rate)},
        "min_peak_explanation": _explanation_json(model, x_min, u_min),
        "max_peak_explanation": _explanation_json(model, x_max, u_max),
    }

    # ---- 账单口径复核：先核票面算式，再核计量证据 ----
    declared_peak = bill["declared_peak_wh"]
    expected_billed = _billed_wh(declared_peak, minimum)
    expected_charge = _charge_cents(expected_billed, rate)
    arithmetic_ok = (bill["declared_billed_wh"] == expected_billed
                     and bill["declared_charge_cents"] == expected_charge)
    report["bill"] = {
        "declared": {
            "peak_quarter_start": _iso(
                model["start"] + timedelta(
                    seconds=bill["peak_quarter"] * QUARTER_SECONDS)),
            "peak_wh": declared_peak,
            "billed_wh": bill["declared_billed_wh"],
            "charge_cents": bill["declared_charge_cents"],
        },
        "minimum_demand_wh": minimum,
        "rate_cents_per_kw": rate,
        "expected_billed_wh": expected_billed,
        "expected_charge_cents": expected_charge,
        "arithmetic_ok": arithmetic_ok,
    }

    if not arithmetic_ok:
        # 计价不符：给出应该填的值；独立的计量范围已在上方给出。
        report["verdict"] = VERDICT_BILLING_MISMATCH
        return report

    # ---- 证据裁定：票面峰值与时段必须在同一份解释里同时成立 ----
    q = bill["peak_quarter"]
    counted = model["counted"]
    # 票面时段用量的可达范围（独立求解，不受票面约束）
    q_cap = min(sum(st["cap"] for st in model["stations"]),
                model["upstream"]["intervals"][alloc.up_of[q]]["delta"])
    lo, hi = 0, q_cap
    while lo < hi:  # u[q] 最小值：最小的 P 使 u[q] <= P 可行
        mid = (lo + hi) // 2
        if alloc.solve({q: (0, mid)}) is not None:
            hi = mid
        else:
            lo = mid + 1
    q_min = lo
    lo, hi = q_min, q_cap
    while lo < hi:  # u[q] 最大值：最大的 P 使 u[q] >= P 可行
        mid = (lo + hi + 1) // 2
        if alloc.solve({q: (mid, alloc.total_wh)}) is not None:
            lo = mid
        else:
            hi = mid - 1
    q_max = lo

    evidence = {"declared_quarter_wh": {"min": q_min, "max": q_max}}

    special = {t: (0, declared_peak) for t in counted}
    special[q] = (declared_peak, declared_peak)
    support = alloc.solve(special)
    if support is None:
        # 没有任何可行解释能同时满足票面峰值和时段
        report["verdict"] = VERDICT_UNSUPPORTED
        report["evidence"] = evidence
        return report

    # 反例：申报时段用量不同，或其他计入时段用量更高
    counterexample = None
    if q_min < declared_peak:
        counterexample = alloc.solve({q: (0, declared_peak - 1)})
    elif q_max > declared_peak:
        counterexample = alloc.solve({q: (declared_peak + 1, alloc.total_wh)})
    elif peak_max > declared_peak:
        counterexample = (x_max, u_max)
    assert counterexample is not None or (
        q_min == q_max == declared_peak and peak_max <= declared_peak)
    if counterexample is None:
        report["verdict"] = VERDICT_CONFIRMED
    else:
        report["verdict"] = VERDICT_UNDETERMINED
        evidence["support_explanation"] = _explanation_json(model, *support)
        evidence["counterexample_explanation"] = _explanation_json(
            model, *counterexample)
    report["evidence"] = evidence
    return report


def audit_bill(state, audit):
    """复核一张上游需量账单，返回可 JSON 序列化的报告（纯函数）。

    :param state: :func:`charging.engine.apply_batch` 产生的 state（只读）。
    :param audit: 审计输入（账期、站点上限、上游读数、账单），结构见
        ``docs/demand.md``。
    :raises DemandError: ``invalid_input``，输入非法时不产生任何部分结果。
    :return: 报告字典；``status`` 为 ``feasible`` 时含独立计量分析与裁定
        （``verdict``），为 ``metering_conflict`` 时不含证据或计价结论。
    """
    model = _parse_audit(state, audit)
    return _audit_model(model)
