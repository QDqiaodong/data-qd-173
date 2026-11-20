"""车队补能与电网降载邀约的联合排程（纯函数、无副作用）。

完整契约见 ``docs/planning.md``。本模块只做**未来计划**：

* 输入车队需求（站点、UTC 窗口、等长时段、每段站点额度、车辆在站窗口与
  补能需求）与电网降载邀约（连续降载时段、逐段固定扣减、报酬）。
* 输出一份常态充电计划、每个邀约的接受/拒绝决定，以及每个已接受邀约被
  激活情景下的应急计划。
* 不发控制指令、不生成计费用的实际电量；与计量结算（``charging.engine``）
  和收款准入（``charging.workbench``）完全解耦，不读写它们的状态。

求解口径：

* 单情景可行性（给定逐段有效额度，可选固定前缀）用整数最大流精确求解，
  所有能量均为整数 Wh，流网络天然给出整数解。
* 多个邀约作为**互斥激活情景**，但它们共享同一份常态计划。一个邀约组合
  可兑现，当且仅当存在一份常态计划，使每个成员邀约的应急计划在激活开始
  前与该常态计划逐车逐段完全相同（前缀锁定）。组合搜索为精确回溯 +
  最大流剪枝（见 :func:`_joint_feasible`），不做任何启发式近似。
"""

from copy import deepcopy

PLAN_VERSION = 1

# 单次联合可行性回溯的节点预算；超出即抛 search_limit_exceeded，绝不发布
# 未经精确验证的计划。典型站点规模（见 docs/planning.md §8）远低于此值。
DEFAULT_SEARCH_LIMIT = 200_000


class PlanningError(ValueError):
    """输入未通过校验或规划本身不可行。

    ``code`` 为稳定的机器可读错误码（CLI 退出码 2 时打印），``message``
    为人类可读说明。两类失败可区分：

    * ``invalid_input``：文件结构、时间边界、整数范围、窗口一致性、重复
      身份或扣减超过正常额度等**非法输入**。
    * ``baseline_infeasible``：输入合法，但常态计划本身无法满足全部车辆。
    * ``offer_infeasible_alone`` / ``search_limit_exceeded``：见文档。
    """

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _require(cond, code, message):
    if not cond:
        raise PlanningError(code, message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value, code, message):
    _require(isinstance(value, dict), code, message)


def _require_str(value, code, message):
    _require(isinstance(value, str) and value != "", code, message)


# ---------------------------------------------------------------------------
# 时间校验：UTC 窗口起点 + 等长时段
# ---------------------------------------------------------------------------

def _parse_utc(value, code, what):
    """要求带显式 UTC 偏移（Z 或 +00:00 等）的 ISO 时间。"""
    _require_str(value, code, f"{what} 必须是带显式 UTC 偏移的 ISO 时间字符串")
    from datetime import datetime
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanningError(code, f"{what} 无法解析: {value!r}") from exc
    _require(d.tzinfo is not None and d.utcoffset() is not None, code,
             f"{what} 必须带显式 UTC 偏移: {value!r}")
    _require(d.utcoffset().total_seconds() == 0, code,
             f"{what} 必须以 UTC 表示: {value!r}")
    return d


def _check_known_keys(obj, allowed, code, what):
    extra = sorted(set(obj) - set(allowed))
    _require(not extra, code, f"{what} 含未知字段: {extra}")


# ---------------------------------------------------------------------------
# 输入解析与校验
# ---------------------------------------------------------------------------

_FLEET_TOP_KEYS = ("station_id", "window_start", "slot_minutes", "normal_capacity_wh", "vehicles")
_VEHICLE_KEYS = ("vehicle_id", "arrival_slot", "departure_slot",
                 "max_charge_wh", "required_energy_wh")
_OFFER_TOP_KEYS = ("station_id", "window_start", "slot_minutes", "offers")
_OFFER_KEYS = ("offer_id", "start_slot", "end_slot", "reduction_wh", "reward")


def _parse_fleet(fleet):
    _require_object(fleet, "invalid_input", "fleet 输入必须是 JSON 对象")
    _check_known_keys(fleet, _FLEET_TOP_KEYS, "invalid_input", "fleet")
    for key in _FLEET_TOP_KEYS:
        _require(key in fleet, "invalid_input", f"fleet 缺少字段 {key}")

    station_id = fleet["station_id"]
    _require_str(station_id, "invalid_input", "station_id 必须是非空字符串")
    window_start = _parse_utc(fleet["window_start"], "invalid_input", "window_start")

    slot_minutes = fleet["slot_minutes"]
    _require(_is_int(slot_minutes) and slot_minutes > 0, "invalid_input",
             "slot_minutes 必须是正整数")

    caps = fleet["normal_capacity_wh"]
    _require(isinstance(caps, list) and len(caps) > 0, "invalid_input",
             "normal_capacity_wh 必须是非空整数数组")
    n_slots = len(caps)
    for i, cap in enumerate(caps):
        _require(_is_int(cap) and cap >= 0, "invalid_input",
                 f"normal_capacity_wh[{i}] 必须是非负整数")

    raw_vehicles = fleet["vehicles"]
    _require(isinstance(raw_vehicles, list) and len(raw_vehicles) > 0, "invalid_input",
             "vehicles 必须是非空数组")

    vehicles = []
    seen = set()
    for idx, v in enumerate(raw_vehicles):
        _require_object(v, "invalid_input", f"vehicles[{idx}] 必须是对象")
        _check_known_keys(v, _VEHICLE_KEYS, "invalid_input", f"vehicles[{idx}]")
        for key in _VEHICLE_KEYS:
            _require(key in v, "invalid_input", f"vehicles[{idx}] 缺少字段 {key}")
        vid = v["vehicle_id"]
        _require_str(vid, "invalid_input", f"vehicles[{idx}].vehicle_id 必须是非空字符串")
        _require(vid not in seen, "invalid_input", f"vehicle_id 重复: {vid!r}")
        seen.add(vid)

        arr, dep = v["arrival_slot"], v["departure_slot"]
        _require(_is_int(arr) and _is_int(dep), "invalid_input",
                 f"车辆 {vid!r} 的 arrival_slot/departure_slot 必须是整数")
        _require(0 <= arr < dep <= n_slots, "invalid_input",
                 f"车辆 {vid!r} 的在站窗口非法: 需 0 <= arrival < departure <= {n_slots}")

        maxes = v["max_charge_wh"]
        _require(isinstance(maxes, list) and len(maxes) == n_slots, "invalid_input",
                 f"车辆 {vid!r} 的 max_charge_wh 必须是长度 {n_slots} 的数组")
        for t, m in enumerate(maxes):
            _require(_is_int(m) and m >= 0, "invalid_input",
                     f"车辆 {vid!r} max_charge_wh[{t}] 必须是非负整数")
            if t < arr or t >= dep:
                _require(m == 0, "invalid_input",
                         f"车辆 {vid!r} 在在站窗口外的 max_charge_wh[{t}] 必须为 0")

        req = v["required_energy_wh"]
        _require(_is_int(req) and req >= 0, "invalid_input",
                 f"车辆 {vid!r} 的 required_energy_wh 必须是非负整数")
        _require(req <= sum(maxes[arr:dep]), "invalid_input",
                 f"车辆 {vid!r} 的需求 {req} Wh 超过其在站窗口内各段上限之和")

        vehicles.append({
            "vehicle_id": vid,
            "arrival_slot": arr,
            "departure_slot": dep,
            "max_charge_wh": list(maxes),
            "required_energy_wh": req,
        })

    return {
        "station_id": station_id,
        "window_start": window_start,
        "slot_minutes": slot_minutes,
        "n_slots": n_slots,
        "normal_capacity_wh": list(caps),
        # 车辆按标识排序，保证数组次序变化不影响结果
        "vehicles": sorted(vehicles, key=lambda x: x["vehicle_id"]),
    }


def _parse_offers(offers_doc, fleet):
    _require_object(offers_doc, "invalid_input", "offers 输入必须是 JSON 对象")
    _check_known_keys(offers_doc, _OFFER_TOP_KEYS, "invalid_input", "offers")
    for key in _OFFER_TOP_KEYS:
        _require(key in offers_doc, "invalid_input", f"offers 缺少字段 {key}")

    _require(offers_doc["station_id"] == fleet["station_id"], "invalid_input",
             "两份文件的 station_id 必须一致")
    offer_start = _parse_utc(offers_doc["window_start"], "invalid_input",
                             "offers.window_start")
    _require(offer_start == fleet["window_start"], "invalid_input",
             "两份文件的 window_start 必须一致（同一 UTC 窗口）")
    _require(offers_doc["slot_minutes"] == fleet["slot_minutes"], "invalid_input",
             "两份文件的 slot_minutes 必须一致")

    raw = offers_doc["offers"]
    _require(isinstance(raw, list), "invalid_input", "offers 必须是数组")
    n_slots = fleet["n_slots"]
    caps = fleet["normal_capacity_wh"]

    offers = []
    seen = set()
    for idx, o in enumerate(raw):
        _require_object(o, "invalid_input", f"offers[{idx}] 必须是对象")
        _check_known_keys(o, _OFFER_KEYS, "invalid_input", f"offers[{idx}]")
        for key in _OFFER_KEYS:
            _require(key in o, "invalid_input", f"offers[{idx}] 缺少字段 {key}")
        oid = o["offer_id"]
        _require_str(oid, "invalid_input", f"offers[{idx}].offer_id 必须是非空字符串")
        _require(oid not in seen, "invalid_input", f"offer_id 重复: {oid!r}")
        seen.add(oid)

        s, e = o["start_slot"], o["end_slot"]
        _require(_is_int(s) and _is_int(e), "invalid_input",
                 f"邀约 {oid!r} 的 start_slot/end_slot 必须是整数")
        _require(0 <= s < e <= n_slots, "invalid_input",
                 f"邀约 {oid!r} 的降载时段非法: 需 0 <= start < end <= {n_slots}")

        red = o["reduction_wh"]
        _require(_is_int(red) and red > 0, "invalid_input",
                 f"邀约 {oid!r} 的 reduction_wh 必须是正整数")
        reward = o["reward"]
        _require(_is_int(reward) and reward > 0, "invalid_input",
                 f"邀约 {oid!r} 的 reward 必须是正整数")

        # 非法输入：扣减大于覆盖时段中任一正常额度（空闲额度也可被扣减，
        # 但扣减后额度不能为负）。
        for t in range(s, e):
            _require(red <= caps[t], "invalid_input",
                     f"邀约 {oid!r} 在时段 {t} 扣减 {red} Wh 超过正常额度 {caps[t]} Wh")

        offers.append({
            "offer_id": oid,
            "start_slot": s,
            "end_slot": e,
            "reduction_wh": red,
            "reward": reward,
        })

    return sorted(offers, key=lambda x: x["offer_id"])


# ---------------------------------------------------------------------------
# 整数最大流：单情景充电可行性
# ---------------------------------------------------------------------------

class _MaxFlow:
    """邻接表残量网络上的 Dinic 整数最大流（确定性）。

    边以固定顺序加入；BFS 分层与阻塞流 DFS 都按邻接表顺序探索，因此对同
    一张图总是得到相同的流，保证输出确定性。边表示与早期 Edmonds-Karp
    实现一致（``[邻点, 残量, 反向边下标]``），上游按残量反推流量的逻辑
    无需改动。
    """

    def __init__(self, n):
        self.n = n
        self.graph = [[] for _ in range(n)]

    def add_edge(self, u, v, cap):
        self.graph[u].append([v, cap, len(self.graph[v])])
        self.graph[v].append([u, 0, len(self.graph[u]) - 1])

    def max_flow(self, source, sink):
        flow = 0
        n = self.n
        while True:
            level = [-1] * n
            level[source] = 0
            queue = [source]
            head = 0
            while head < len(queue):
                u = queue[head]
                head += 1
                for v, cap, _ in self.graph[u]:
                    if cap > 0 and level[v] == -1:
                        level[v] = level[u] + 1
                        queue.append(v)
            if level[sink] == -1:
                return flow
            it = [0] * n
            inf = float("inf")

            def dfs(v, pushed):
                if v == sink or pushed == 0:
                    return pushed
                while it[v] < len(self.graph[v]):
                    edge = self.graph[v][it[v]]
                    w, cap, rev = edge
                    if cap > 0 and level[w] == level[v] + 1:
                        tr = dfs(w, min(pushed, cap))
                        if tr:
                            edge[1] -= tr
                            self.graph[w][rev][1] += tr
                            return tr
                    it[v] += 1
                return 0

            while True:
                add = dfs(source, inf)
                if not add:
                    break
                flow += add
        return flow


def _effective_caps(n_slots, normal_caps, offer):
    """某邀约激活情景下的逐段有效站点额度。"""
    caps = list(normal_caps)
    if offer is not None:
        for t in range(offer["start_slot"], offer["end_slot"]):
            caps[t] -= offer["reduction_wh"]
    return caps


def solve_scenario(fleet, offer, fixed=None):
    """求解一个情景；可行返回逐车逐段充电矩阵，不可行返回 ``None``。

    :param fleet: :func:`_parse_fleet` 的产物。
    :param offer: ``None`` 表示常态；否则为邀约字典，逐段扣减站点额度。
    :param fixed: ``{slot: {vehicle_id: wh}}``，这些时段的逐车充电量已实际
        安排、必须原样保留（用于激活开始前的共同前缀）。
    """
    n_slots = fleet["n_slots"]
    vehicles = fleet["vehicles"]
    caps = _effective_caps(n_slots, fleet["normal_capacity_wh"], offer)
    fixed = fixed or {}

    # 固定前缀先占用站点额度，并预先满足一部分车辆需求。
    remaining_caps = list(caps)
    remaining_need = []
    for v in vehicles:
        pre = 0
        for t, alloc in fixed.items():
            if t < v["arrival_slot"] or t >= v["departure_slot"]:
                continue
            x = alloc.get(v["vehicle_id"], 0)
            if x < 0 or x > v["max_charge_wh"][t]:
                return None
            pre += x
        if pre > v["required_energy_wh"]:
            return None
        remaining_need.append(v["required_energy_wh"] - pre)
    for t, alloc in fixed.items():
        used = sum(alloc.values())
        if used < 0 or used > caps[t]:
            return None
        remaining_caps[t] = caps[t] - used

    total_need = sum(remaining_need)
    if total_need == 0:
        return [[0] * n_slots for _ in vehicles]

    # 节点：source=0，车辆 1..V，时段 1+V..1+V+T，sink 最后。
    v_of = lambda i: 1 + i
    t_of = lambda t: 1 + len(vehicles) + t
    source = 0
    sink = 1 + len(vehicles) + n_slots
    mf = _MaxFlow(sink + 1)

    for i, v in enumerate(vehicles):
        if remaining_need[i] > 0:
            mf.add_edge(source, v_of(i), remaining_need[i])
        for t in range(v["arrival_slot"], v["departure_slot"]):
            if t in fixed:
                continue  # 固定时段不再参与重新调度
            cap = min(v["max_charge_wh"][t], remaining_caps[t])
            if cap > 0:
                mf.add_edge(v_of(i), t_of(t), cap)
    for t in range(n_slots):
        if t not in fixed and remaining_caps[t] > 0:
            mf.add_edge(t_of(t), sink, remaining_caps[t])

    flow = mf.max_flow(source, sink)
    if flow != total_need:
        return None

    matrix = [[0] * n_slots for _ in vehicles]
    for i, v in enumerate(vehicles):
        for (to, residual_cap, rev) in mf.graph[v_of(i)]:
            if 1 + len(vehicles) <= to <= sink - 1:
                t = to - (1 + len(vehicles))
                # 原始容量 -> 已用流量
                original = residual_cap + mf.graph[to][rev][1]
                used = original - residual_cap
                matrix[i][t] = used
    return matrix


def _slot_usage(matrix, n_slots):
    """逐车矩阵 -> 逐段站点使用量。"""
    usage = [0] * n_slots
    for row in matrix:
        for t, x in enumerate(row):
            usage[t] += x
    return usage


# ---------------------------------------------------------------------------
# 组合可兑现性：共享常态计划 + 每情景完整前缀锁定的精确回溯
# ---------------------------------------------------------------------------

class _FlowOracle:
    """单一情景的后缀最大流可行性预言机（随 DFS 逐段收紧）。

    正确性口径与 :func:`solve_scenario` 一致：给定已固定前缀，在剩余后缀
    上建整数流网络，流值等于总剩余需求当且仅当后缀可补全。增量性体现在
    网络只包含**尚未固定**的时段——每深入一个 DFS 时段，网络就少一列
    时段节点，构建与增广成本随深度下降；DFS 回滚只需丢弃当前网络引用。
    """

    def __init__(self, fleet, offer):
        self.fleet = fleet
        self.offer = offer
        self.vehicles = fleet["vehicles"]
        self.v_count = len(self.vehicles)
        self.n_slots = fleet["n_slots"]
        self.eff = _effective_caps(self.n_slots, fleet["normal_capacity_wh"], offer)
        self._memo = {}

    def feasible(self, fixed, placed):
        # 同一固定前缀（逐车已安排量 + 各时段站点使用量）的可行性是确定的，
        # 记忆化可让 DFS 中经不同分支到达的相同前缀只算一次最大流。
        key = (tuple(placed),
               tuple(sum(a.values()) for _, a in sorted(fixed.items())))
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        result = self._compute(fixed, placed)
        self._memo[key] = result
        return result

    def _compute(self, fixed, placed):
        remaining_caps = list(self.eff)
        for t, alloc in fixed.items():
            used = sum(alloc.values())
            if used > self.eff[t]:
                return False
            remaining_caps[t] = self.eff[t] - used
        remaining_need = [
            v["required_energy_wh"] - placed[i]
            for i, v in enumerate(self.vehicles)]
        if any(x < 0 for x in remaining_need):
            return False
        total_need = sum(remaining_need)
        if total_need == 0:
            return True

        # 只把“仍有需求的车”与“未固定且仍被某辆车覆盖的时段”放进网络，
        # 避免在长窗口（如 96 段）上为无关车/无关段建边。
        active_vehicles = [i for i, need in enumerate(remaining_need) if need > 0]
        live_slots = sorted({
            t for i in active_vehicles
            for t in range(self.vehicles[i]["arrival_slot"],
                           self.vehicles[i]["departure_slot"])
            if t not in fixed})
        slot_index = {t: k for k, t in enumerate(live_slots)}
        v_count = len(active_vehicles)
        v_of = lambda k: 1 + k
        t_of = lambda k: 1 + v_count + k
        source, sink = 0, 1 + v_count + len(live_slots)
        mf = _MaxFlow(sink + 1)
        for k, i in enumerate(active_vehicles):
            mf.add_edge(source, v_of(k), remaining_need[i])
            v = self.vehicles[i]
            for t in range(v["arrival_slot"], v["departure_slot"]):
                if t in fixed:
                    continue
                cap = min(v["max_charge_wh"][t], remaining_caps[t])
                if cap > 0:
                    mf.add_edge(v_of(k), t_of(slot_index[t]), cap)
        for t in live_slots:
            if remaining_caps[t] > 0:
                mf.add_edge(t_of(slot_index[t]), sink, remaining_caps[t])
        return mf.max_flow(source, sink) == total_need


def _search_baseline_prefix(fleet, members, search_limit, want_solution=False):
    """为邀约组合 ``members`` 寻找一份满足全部前缀锁定的常态前缀。

    组合可兑现的精确含义：存在一份常态计划，使每个成员邀约 ``o`` 的应急
    计划在其激活开始前（时段 ``0 .. start_slot-1``）与该常态计划**逐车逐段
    完全相同**。注意激活较晚的邀约锁定更长的前缀——不仅是最早激活段之前
    的共享段，还包括两次激活之间的常态段。

    本回溯枚举常态计划在 ``0 .. max(start_slot)-1`` 全部耦合时段上的逐车
    分配。每个 DFS 节点先做 O(车辆) 的剩余容量必要性预检，通过后再让各
    情景的 :class:`_FlowOracle` 在“仅含未固定时段”的收缩网络上跑一次整数
    最大流。随深度增加网络逐列缩小，判定精确；前缀之外各情景互不耦合、
    可独立重排。

    :param want_solution: 为真时在首个可行解处返回固定前缀字典，否则只
        返回布尔可行性。
    """
    # start_slot == 0 的邀约没有任何已安排前缀，与其它情景无耦合，不参与
    # 联合回溯（其单项可行性已在调用方过滤）。
    coupled = sorted((o for o in members if o["start_slot"] > 0),
                     key=lambda o: (o["start_slot"], o["offer_id"]))
    if not coupled:
        return {} if want_solution else True

    vehicles = fleet["vehicles"]
    n_slots = fleet["n_slots"]
    coupled_len = max(o["start_slot"] for o in coupled)
    budget = {"nodes": 0}
    placed = [0] * len(vehicles)
    fixed = {}

    oracles = [_FlowOracle(fleet, None)] + [
        _FlowOracle(fleet, o) for o in coupled]
    suffix_room, suffix_cap = {}, {}
    for orc in oracles:
        oid = "__base__" if orc.offer is None else orc.offer["offer_id"]
        run = 0
        for t in range(n_slots - 1, -1, -1):
            run += orc.eff[t]
            suffix_cap[(oid, t)] = run
        for i, v in enumerate(vehicles):
            run = 0
            for t in range(n_slots - 1, -1, -1):
                if v["arrival_slot"] <= t < v["departure_slot"]:
                    run += v["max_charge_wh"][t]
                suffix_room[(oid, t, i)] = run

    def cheap_prefilter(t):
        for orc in oracles:
            if orc.offer is not None and orc.offer["start_slot"] <= t:
                continue
            oid = "__base__" if orc.offer is None else orc.offer["offer_id"]
            total_remaining = sum(v["required_energy_wh"] - placed[i]
                                  for i, v in enumerate(vehicles))
            if total_remaining > suffix_cap[(oid, t + 1)]:
                return False
            for i, v in enumerate(vehicles):
                rem = v["required_energy_wh"] - placed[i]
                if rem < 0 or rem > suffix_room[(oid, t + 1, i)]:
                    return False
        return True

    def flows_ok(t):
        for orc in oracles:
            if orc.offer is not None and orc.offer["start_slot"] <= t:
                continue
            if not orc.feasible(fixed, placed):
                return False
        return True

    def dfs(t):
        budget["nodes"] += 1
        if budget["nodes"] > search_limit:
            raise PlanningError(
                "search_limit_exceeded",
                f"联合可行性回溯超过 {search_limit} 个节点；请缩小窗口/车辆/邀约规模，"
                "或通过 --search-limit 提高节点预算")

        if t == coupled_len:
            # 预言机已逐段确认；最终再用独立全量最大流兜底，防止任何偏差。
            if solve_scenario(fleet, None, dict(fixed)) is None:
                return False
            for o in coupled:
                locked = {u: fixed[u] for u in range(o["start_slot"])}
                if solve_scenario(fleet, o, locked) is None:
                    return False
            return True

        present = []
        for i, v in enumerate(vehicles):
            if not (v["arrival_slot"] <= t < v["departure_slot"]):
                continue
            need = v["required_energy_wh"] - placed[i]
            later_room = sum(v["max_charge_wh"][u]
                             for u in range(t + 1, v["departure_slot"]))
            if need > v["max_charge_wh"][t] + later_room:
                return False
            lo = max(0, need - later_room)
            hi = min(v["max_charge_wh"][t], need)
            present.append((i, lo, hi, later_room))
        present.sort(key=lambda b: (b[3], b[2] - b[1],
                                    vehicles[b[0]]["vehicle_id"]))

        caps = fleet["normal_capacity_wh"][t]

        def branch(pi, budget_left, alloc):
            if pi == len(present):
                return commit(alloc)
            i, lo, hi, _ = present[pi]
            vid = vehicles[i]["vehicle_id"]
            upper = min(hi, budget_left)
            if lo > upper:
                return False
            for x in range(lo, upper + 1):
                alloc[vid] = x
                if branch(pi + 1, budget_left - x, alloc):
                    return True
            alloc.pop(vid, None)
            return False

        def commit(alloc):
            fixed[t] = dict(alloc)
            for i, _, _, _ in present:
                placed[i] += alloc.get(vehicles[i]["vehicle_id"], 0)
            ok = cheap_prefilter(t) and flows_ok(t) and dfs(t + 1)
            if ok and want_solution:
                return True
            for i, _, _, _ in present:
                placed[i] -= alloc.get(vehicles[i]["vehicle_id"], 0)
            del fixed[t]
            return ok

        return branch(0, caps, {})

    if not dfs(0):
        return False
    return deepcopy(fixed) if want_solution else True


def _find_best_combination(fleet, candidates, search_limit):
    """在所有可兑现组合中选报酬最高、并列取标识序列字典序最小者。

    利用两条精确性质做分支限界：

    * 单项不可兑现的邀约已在调用方剔除；``start_slot == 0`` 的邀约无锁定
      前缀，与任何其它情景都不冲突，始终可组合。
    * 可兑现性关于集合**单调**：任一不可兑现子集的超集也不可兑现（同一份
      常态前缀必须同时满足更多情景）。可行性结果按集合记忆化。
    """
    # 报酬降序排列，使包含分支尽快拿到高报酬，强化上界剪枝。
    ordered = sorted(candidates, key=lambda o: (-o["reward"], o["offer_id"]))
    m = len(ordered)
    suffix_reward = [0] * (m + 1)
    for i in range(m - 1, -1, -1):
        suffix_reward[i] = suffix_reward[i + 1] + ordered[i]["reward"]

    memo = {}
    infeasible_keys = set()

    def feasible(subset):
        coupled = [o for o in subset if o["start_slot"] > 0]
        if len(coupled) <= 1:
            return True  # 单项已验证；无耦合或单成员只取决于其自身可行性
        key = frozenset(o["offer_id"] for o in coupled)
        cached = memo.get(key)
        if cached is not None:
            return cached
        if any(bad <= key for bad in infeasible_keys):
            memo[key] = False
            return False
        ok = bool(_search_baseline_prefix(fleet, coupled, search_limit))
        memo[key] = ok
        if not ok:
            infeasible_keys.add(key)
        return ok

    best = {"reward": -1, "ids": None, "subset": []}

    def dfs(i, chosen, reward):
        if reward + suffix_reward[i] < best["reward"]:
            return
        if i == m:
            if not chosen:
                return
            if not feasible(chosen):
                return
            ids = tuple(sorted(o["offer_id"] for o in chosen))
            if (reward > best["reward"]
                    or (reward == best["reward"]
                        and (best["ids"] is None or ids < best["ids"]))):
                best.update(reward=reward, ids=ids, subset=list(chosen))
            return
        # 先尝试包含（ordered 已按报酬降序），再尝试排除。
        cand = ordered[i]
        dfs(i + 1, chosen + [cand], reward + cand["reward"])
        dfs(i + 1, chosen, reward)

    dfs(0, [], 0)
    return best["subset"]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def plan(fleet_input, offers_input, search_limit=DEFAULT_SEARCH_LIMIT):
    """把车队需求与降载邀约编成可核查的排程与邀约决策（纯函数）。

    :raises PlanningError: 输入非法（``invalid_input``）或常态不可行
        （``baseline_infeasible``），或回溯超预算（``search_limit_exceeded``）。
    :return: 可直接 JSON 序列化的结果字典（结构见 docs/planning.md §6）。
    """
    fleet = _parse_fleet(fleet_input)
    offers = _parse_offers(offers_input, fleet)

    # 1) 常态计划必须存在，否则不发布任何缺车/超额计划。
    baseline_matrix = solve_scenario(fleet, None)
    if baseline_matrix is None:
        raise PlanningError(
            "baseline_infeasible",
            "常态情景下无法在站点额度与车辆窗口内满足全部车辆需求")

    # 2) 单项可兑现性（也是非法输入与业务不可接受的分界记录）。
    alone_feasible = {}
    for o in offers:
        alone_feasible[o["offer_id"]] = solve_scenario(fleet, o) is not None

    # 3) 精确分支限界：选报酬最高的可兑现组合；并列按标识序列字典序。
    #    单项不可兑现的邀约不可能出现在任何可兑现组合中（它在自身激活
    #    情景下就无解），先剔除以缩小搜索空间。
    candidates = [o for o in offers if alone_feasible[o["offer_id"]]]
    accepted = _find_best_combination(fleet, candidates, search_limit)
    accepted_ids = {o["offer_id"] for o in accepted}

    # 4) 为已接受组合重建一份满足全部前缀锁定的共享常态计划，再据此构造
    #    每个成员的应急计划（其激活前逐车逐段与常态完全相同）。
    if accepted:
        prefix_fixed = _search_baseline_prefix(
            fleet, accepted, search_limit, want_solution=True)
        baseline_tail = solve_scenario(fleet, None, prefix_fixed)
        baseline_matrix = _merge_fixed(fleet, prefix_fixed, baseline_tail)

    scenarios = [_scenario_view("baseline", None, fleet, baseline_matrix,
                                prefix_locked=None)]

    decisions = []
    for o in offers:
        oid = o["offer_id"]
        is_accepted = oid in accepted_ids
        reason = None
        if not is_accepted:
            reason = ("infeasible_alone" if not alone_feasible[oid]
                      else "not_in_best_combination")
        decisions.append({
            "offer_id": oid,
            "accepted": is_accepted,
            "reward_if_accepted": o["reward"] if is_accepted else 0,
            "reject_reason": reason,
            "feasible_alone": alone_feasible[oid],
        })
        if is_accepted:
            locked = {t: _row_map(fleet, baseline_matrix, t)
                      for t in range(o["start_slot"])}
            cont_tail = solve_scenario(fleet, o, locked)
            if cont_tail is None:  # 理论上不可达：组合已通过精确验证
                raise PlanningError("search_limit_exceeded",
                                    f"邀约 {oid!r} 应急计划构造失败（内部错误）")
            cont_matrix = _merge_fixed(fleet, locked, cont_tail)
            scenarios.append(_scenario_view(
                f"offer:{oid}", o, fleet, cont_matrix,
                prefix_locked=o["start_slot"]))

    result = {
        "plan_version": PLAN_VERSION,
        "station_id": fleet["station_id"],
        "window_start": fleet["window_start"].isoformat(),
        "slot_minutes": fleet["slot_minutes"],
        "slot_count": fleet["n_slots"],
        "normal_capacity_wh": fleet["normal_capacity_wh"],
        "decision": {
            "accepted_offer_ids": sorted(accepted_ids),
            "total_reward": sum(o["reward"] for o in accepted),
            "offer_count": len(offers),
            "accepted_count": len(accepted),
        },
        "offers": [_offer_view(o) for o in offers],
        "offer_decisions": decisions,
        "scenarios": scenarios,
    }
    return result


def _row_map(fleet, matrix, t):
    """固定时段 t 的逐车分配（vehicle_id -> wh）。"""
    return {fleet["vehicles"][i]["vehicle_id"]: matrix[i][t]
            for i in range(len(fleet["vehicles"])) if matrix[i][t] > 0}


def _merge_fixed(fleet, fixed, tail_matrix):
    """把固定前缀与最大流给出的尾部矩阵合并成完整矩阵。"""
    matrix = [list(row) for row in tail_matrix]
    id_index = {v["vehicle_id"]: i for i, v in enumerate(fleet["vehicles"])}
    for t, alloc in fixed.items():
        for vid, wh in alloc.items():
            matrix[id_index[vid]][t] = wh
    return matrix


def _offer_view(o):
    return {
        "offer_id": o["offer_id"],
        "start_slot": o["start_slot"],
        "end_slot": o["end_slot"],
        "reduction_wh": o["reduction_wh"],
        "reward": o["reward"],
    }


def _scenario_view(scenario_id, offer, fleet, matrix, prefix_locked):
    n_slots = fleet["n_slots"]
    usage = _slot_usage(matrix, n_slots)
    effective_caps = _effective_caps(n_slots, fleet["normal_capacity_wh"], offer)
    vehicles_view = []
    for i, v in enumerate(fleet["vehicles"]):
        vehicles_view.append({
            "vehicle_id": v["vehicle_id"],
            "arrival_slot": v["arrival_slot"],
            "departure_slot": v["departure_slot"],
            "required_energy_wh": v["required_energy_wh"],
            "planned_energy_wh": sum(matrix[i]),
            "charge_wh_by_slot": list(matrix[i]),
        })
    slots = []
    for t in range(n_slots):
        slots.append({
            "slot": t,
            "normal_capacity_wh": fleet["normal_capacity_wh"][t],
            "reduction_wh": (offer["reduction_wh"]
                             if offer is not None and offer["start_slot"] <= t < offer["end_slot"]
                             else 0),
            "effective_capacity_wh": effective_caps[t],
            "station_used_wh": usage[t],
            "spare_capacity_wh": effective_caps[t] - usage[t],
        })
    view = {
        "scenario_id": scenario_id,
        "kind": "baseline" if offer is None else "contingency",
        "offer_id": None if offer is None else offer["offer_id"],
        "activation_start_slot": None if offer is None else offer["start_slot"],
        "prefix_locked_through_slot": prefix_locked,
        "vehicles": vehicles_view,
        "slots": slots,
        "total_planned_wh": sum(usage),
    }
    return view
