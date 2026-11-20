"""闭账周期充电报表的保密发布编制（纯函数、只读、不写回结算状态）。

完整契约见 ``docs/reporting.md``。本模块与计量结算（``charging.engine``）、
收款准入（``charging.workbench``）、排程（``charging.planning``）完全解耦：

* 直接读取 :func:`charging.engine.apply_batch` 产生的 state，只取目标闭账
  周期 ``closed_lines`` 中 ``billable=true`` 的整数 ``energy_wh``，站点从
  ``sessions`` 反查，车队归属由请求文件对合格会话唯一给出；
* 公开事实（上限 + 历史精确值 + 本轮拟发布值）构成站点×车队整数矩阵上的
  线性约束，保密余量用带下界整数流精确校验（见 :mod:`marginflow`），
  不枚举任何 Wh 取值；
* 候选发布包是全有或全无的集合，发布组合用精确分支限界求总价值最高者，
  并列按排序后包标识序列的字典序裁决；不做逐包独立审批，也不做贪心。
"""

from .marginflow import MarginFlow

REPORT_VERSION = 1


class ReportingError(ValueError):
    """请求未通过校验或发布前已存在保密风险。

    ``code`` 为稳定机器可读错误码（CLI 退出码 2 时打印），``message`` 为
    人类可读说明：

    * ``invalid_input``        —— 结构、坐标、整数范围、周期状态等非法输入；
    * ``conflicting_history``  —— 历史已公开事实互相矛盾或与本期汇总冲突；
    * ``prior_risk``           —— 仅凭公开上限与历史事实，保密余量已不成立。
    """

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _require(cond, message):
    if not cond:
        raise ReportingError("invalid_input", message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value, what):
    _require(isinstance(value, dict), f"{what} 必须是 JSON 对象")


def _require_str(value, what):
    _require(isinstance(value, str) and value != "", f"{what} 必须是非空字符串")


def _check_keys(obj, allowed, what):
    extra = sorted(set(obj) - set(allowed))
    _require(not extra, f"{what} 含未知字段: {extra}")


# ---------------------------------------------------------------------------
# 1) 从闭账 state 编制本期站点×车队真实用量矩阵
# ---------------------------------------------------------------------------

def _aggregate_closed_period(state, period_id, fleet_map):
    _require_object(state, "state")
    _require(state.get("version") == 1, "state.version 不受支持（仅支持 version=1）")
    periods = state.get("periods")
    _require(isinstance(periods, list), "state.periods 必须是数组")
    period = next((p for p in periods if p.get("period_id") == period_id), None)
    if period is None:
        raise ReportingError("invalid_input", f"未知周期 {period_id!r}：state 中不存在该 period_id")
    if period.get("status") != "closed":
        raise ReportingError("invalid_input",
                            f"周期 {period_id!r} 当前状态为 {period.get('status')!r}，本入口只处理已关闭周期")

    sessions = state.get("sessions")
    closed_lines = state.get("closed_lines")
    _require(isinstance(sessions, dict) and isinstance(closed_lines, dict),
             "state 缺少 sessions/closed_lines 结构")

    grid = {}          # (station_id, fleet_id) -> 整数 Wh
    touched_sessions = set()
    for line in closed_lines.values():
        if line.get("period_id") != period_id:
            continue
        touched_sessions.add(line.get("session_id"))
        if not line.get("billable"):
            continue  # 未计费行不参与；后续金额调整同样不参与
        energy = line.get("energy_wh")
        _require(_is_int(energy) and energy >= 0,
                 f"闭账行 {line.get('line_id')!r} 的 energy_wh 必须是非负整数")
        sid = line.get("session_id")
        sess = sessions.get(sid)
        _require(sess is not None, f"闭账行 {line.get('line_id')!r} 的会话 {sid!r} 在 state.sessions 中不存在")
        station_id = sess.get("station_id")
        fleet_id = fleet_map.get(sid)
        _require(fleet_id is not None,
                 f"合格会话 {sid!r} 缺少车队归属（fleet_attribution 必须覆盖全部 billable 会话）")
        key = (station_id, fleet_id)
        grid[key] = grid.get(key, 0) + energy

    # 归属文件里指向不存在/非本期会话的条目也是非法输入
    for sid in fleet_map:
        _require(sid in sessions, f"车队归属引用了 state 中不存在的会话 {sid!r}")
    return grid


# ---------------------------------------------------------------------------
# 2) 请求解析与校验
# ---------------------------------------------------------------------------

_TOP_KEYS = ("period_id", "fleet_attribution", "public_caliber",
             "historical_facts", "candidate_packages", "protected_cells")
_CALIBER_KEYS = ("stations", "fleets", "upper_bounds_wh")
_BOUND_KEYS = ("station_id", "fleet_id", "upper_bound_wh")
_FACT_KEYS = ("fact_type", "station_id", "fleet_id", "value_wh")
_PACKAGE_KEYS = ("package_id", "value", "requests")
_PROTECTED_KEYS = ("station_id", "fleet_id", "margin_wh")


def _parse_request(request):
    _require_object(request, "request")
    _check_keys(request, _TOP_KEYS, "request")
    for key in _TOP_KEYS:
        _require(key in request, f"request 缺少字段 {key!r}")
    period_id = request["period_id"]
    _require_str(period_id, "period_id")

    # 车队归属：合格会话 -> 唯一车队
    raw_attr = request["fleet_attribution"]
    _require(isinstance(raw_attr, list) and raw_attr,
             "fleet_attribution 必须是非空数组")
    fleet_map = {}
    for idx, item in enumerate(raw_attr):
        _require_object(item, f"fleet_attribution[{idx}]")
        _check_keys(item, ("session_id", "fleet_id"), f"fleet_attribution[{idx}]")
        sid, fid = item["session_id"], item["fleet_id"]
        _require_str(sid, f"fleet_attribution[{idx}].session_id")
        _require_str(fid, f"fleet_attribution[{idx}].fleet_id")
        _require(sid not in fleet_map, f"会话 {sid!r} 被赋予了多个车队归属")
        fleet_map[sid] = fid

    # 公开口径
    caliber = request["public_caliber"]
    _require_object(caliber, "public_caliber")
    _check_keys(caliber, _CALIBER_KEYS, "public_caliber")
    stations, fleets = caliber["stations"], caliber["fleets"]
    _require(isinstance(stations, list) and stations, "public_caliber.stations 必须是非空数组")
    _require(isinstance(fleets, list) and fleets, "public_caliber.fleets 必须是非空数组")
    station_set, fleet_set = set(), set()
    for s in stations:
        _require_str(s, "public_caliber.stations[]")
        _require(s not in station_set, f"公开口径站点重复: {s!r}")
        station_set.add(s)
    for f in fleets:
        _require_str(f, "public_caliber.fleets[]")
        _require(f not in fleet_set, f"公开口径车队重复: {f!r}")
        fleet_set.add(f)

    raw_bounds = caliber["upper_bounds_wh"]
    _require(isinstance(raw_bounds, list) and raw_bounds,
             "upper_bounds_wh 必须是非空数组")
    bound_pairs = set()
    upper = {}
    for idx, item in enumerate(raw_bounds):
        _require_object(item, f"upper_bounds_wh[{idx}]")
        _check_keys(item, _BOUND_KEYS, f"upper_bounds_wh[{idx}]")
        s, f, u = item["station_id"], item["fleet_id"], item["upper_bound_wh"]
        _require_str(s, f"upper_bounds_wh[{idx}].station_id")
        _require_str(f, f"upper_bounds_wh[{idx}].fleet_id")
        _require(_is_int(u) and u >= 0,
                 f"upper_bounds_wh[{idx}].upper_bound_wh 必须是非负整数")
        _require(s in station_set and f in fleet_set,
                 f"上限坐标 ({s!r},{f!r}) 不在公开口径的站点/车队清单内（未知坐标）")
        pair = (s, f)
        _require(pair not in bound_pairs, f"上限坐标重复: {pair}")
        bound_pairs.add(pair)
        upper[pair] = u
    # 口径必须覆盖参与统计的全部站点×车队组合（缺格视为非法，避免静默口径）
    missing = sorted((s, f) for s in station_set for f in fleet_set
                     if (s, f) not in bound_pairs)
    _require(not missing, f"公开口径缺少组合上限: {missing}")

    return period_id, fleet_map, stations, fleets, station_set, fleet_set, upper


def _normalize_fact(item, what, station_set, fleet_set, known_values=None):
    """把一条精确事实规范化为 ``(kind, station, fleet, value)`` 并做坐标校验。

    ``known_values`` 给出时做跨条目去重/冲突检测，返回规范元组；否则只
    解析并返回（用于包请求，发布值由内部汇总另行填充）。
    """
    _require_object(item, what)
    _check_keys(item, _FACT_KEYS, what)
    kind = item["fact_type"]
    _require(kind in ("cell", "station", "fleet", "total"),
             f"{what}.fact_type 非法: {kind!r}（允许 cell/station/fleet/total）")
    s, f, v = item.get("station_id"), item.get("fleet_id"), item.get("value_wh")
    if kind == "cell":
        _require_str(s, f"{what}.station_id")
        _require_str(f, f"{what}.fleet_id")
        _require(s in station_set and f in fleet_set,
                 f"{what} 坐标 ({s!r},{f!r}) 不在公开口径内（未知坐标）")
        key = ("cell", s, f)
    elif kind == "station":
        _require_str(s, f"{what}.station_id")
        _require(f is None, f"{what} 为站点小计，fleet_id 必须为 null")
        _require(s in station_set, f"{what} 未知站点 {s!r}")
        key = ("station", s, None)
    elif kind == "fleet":
        _require_str(f, f"{what}.fleet_id")
        _require(s is None, f"{what} 为车队小计，station_id 必须为 null")
        _require(f in fleet_set, f"{what} 未知车队 {f!r}")
        key = ("fleet", None, f)
    else:
        _require(s is None and f is None, f"{what} 为全表总计，坐标必须为 null")
        key = ("total", None, None)
    _require(_is_int(v) and v >= 0, f"{what}.value_wh 必须是非负整数")
    if known_values is not None:
        if key in known_values:
            if known_values[key] != v:
                raise ReportingError(
                    "conflicting_history",
                    f"同一事实 {key} 出现互相冲突的公开数值: "
                    f"{known_values[key]} != {v}")
        else:
            known_values[key] = v
    return (kind, s, f, v), key


def _parse_history(request, station_set, fleet_set, upper, grid):
    raw = request["historical_facts"]
    _require(isinstance(raw, list), "historical_facts 必须是数组")
    known = {}
    facts = []
    for idx, item in enumerate(raw):
        fact, key = _normalize_fact(item, f"historical_facts[{idx}]",
                                    station_set, fleet_set, known)
        facts.append(fact)
    # 历史值越过公开上限 / 与本期内部汇总不一致
    for kind, s, f, v in facts:
        if kind == "cell":
            _require(v <= upper[(s, f)],
                     f"历史事实 {kind} ({s!r},{f!r})={v} 越过公开上限 {upper[(s, f)]}")
            if v != grid.get((s, f), 0):
                raise ReportingError(
                    "conflicting_history",
                    f"历史已公开格值 ({s!r},{f!r})={v} 与本期汇总 "
                    f"{grid.get((s, f), 0)} 不一致；既有数值不能撤下或改写")
        elif kind == "station":
            cap = sum(upper[(s, g)] for g in fleet_set)
            _require(v <= cap, f"历史站点小计 {s!r}={v} 越过该行公开上限之和 {cap}")
            internal = sum(w for (st, _), w in grid.items() if st == s)
            if v != internal:
                raise ReportingError(
                    "conflicting_history",
                    f"历史已公开站点小计 {s!r}={v} 与本期汇总 {internal} 不一致")
        elif kind == "fleet":
            cap = sum(upper[(g_station, f)] for g_station in station_set)
            _require(v <= cap, f"历史车队小计 {f!r}={v} 越过该列公开上限之和 {cap}")
            internal = sum(w for (_, fl), w in grid.items() if fl == f)
            if v != internal:
                raise ReportingError(
                    "conflicting_history",
                    f"历史已公开车队小计 {f!r}={v} 与本期汇总 {internal} 不一致")
        else:
            cap = sum(upper.values())
            _require(v <= cap, f"历史全表总计 {v} 越过全部公开上限之和 {cap}")
            internal = sum(grid.values())
            if v != internal:
                raise ReportingError(
                    "conflicting_history",
                    f"历史已公开全表总计 {v} 与本期汇总 {internal} 不一致")
    return facts


def _parse_packages(request, station_set, fleet_set):
    raw = request["candidate_packages"]
    _require(isinstance(raw, list), "candidate_packages 必须是数组")
    packages = []
    seen_ids = set()
    for idx, pack in enumerate(raw):
        _require_object(pack, f"candidate_packages[{idx}]")
        _check_keys(pack, _PACKAGE_KEYS, f"candidate_packages[{idx}]")
        pid = pack["package_id"]
        _require_str(pid, f"candidate_packages[{idx}].package_id")
        _require(pid not in seen_ids, f"候选包标识重复: {pid!r}")
        seen_ids.add(pid)
        value = pack["value"]
        _require(_is_int(value) and value > 0,
                 f"候选包 {pid!r} 的 value 必须是正整数")
        requests = pack["requests"]
        _require(isinstance(requests, list) and requests,
                 f"候选包 {pid!r} 的 requests 必须是非空数组")
        keys = []
        for j, item in enumerate(requests):
            _, key = _normalize_fact(item, f"candidate_packages[{pid!r}].requests[{j}]",
                                     station_set, fleet_set)
            _require(key not in keys, f"候选包 {pid!r} 内部重复请求同一事实 {key}")
            keys.append(key)
        packages.append({"package_id": pid, "value": value,
                         "requests": requests, "keys": keys})
    return packages


def _parse_protected(request, station_set, fleet_set):
    raw = request["protected_cells"]
    _require(isinstance(raw, list) and raw, "protected_cells 必须是非空数组")
    cells = []
    seen = set()
    for idx, item in enumerate(raw):
        _require_object(item, f"protected_cells[{idx}]")
        _check_keys(item, _PROTECTED_KEYS, f"protected_cells[{idx}]")
        s, f, d = item["station_id"], item["fleet_id"], item["margin_wh"]
        _require_str(s, f"protected_cells[{idx}].station_id")
        _require_str(f, f"protected_cells[{idx}].fleet_id")
        _require(s in station_set and f in fleet_set,
                 f"保护格坐标 ({s!r},{f!r}) 不在公开口径内（未知坐标）")
        _require(_is_int(d) and d > 0,
                 f"protected_cells[{idx}].margin_wh 必须是正整数")
        pair = (s, f)
        _require(pair not in seen, f"保护格重复: {pair}")
        seen.add(pair)
        cells.append({"station_id": s, "fleet_id": f, "margin_wh": d})
    return cells


# ---------------------------------------------------------------------------
# 3) 内部汇总 -> 拟发布事实取值
# ---------------------------------------------------------------------------

def _fact_value(kind, s, f, grid, stations, fleets):
    if kind == "cell":
        return grid.get((s, f), 0)
    if kind == "station":
        return sum(w for (st, _), w in grid.items() if st == s)
    if kind == "fleet":
        return sum(w for (_, fl), w in grid.items() if fl == f)
    return sum(grid.values())


def _build_fact(request_fact, grid, stations, fleets):
    kind, s, f, _ = request_fact
    return (kind, s, f, _fact_value(kind, s, f, grid, stations, fleets))


# ---------------------------------------------------------------------------
# 4) 保密校验与组合搜索
# ---------------------------------------------------------------------------

def _evaluate_fact_set(fact_set, upper, stations, fleets, protected, grid,
                       s_index, f_index, cache):
    """一组公开精确事实下，全部保护格是否两侧余量都成立。

    事实集合相同则结果记忆化；流网络只建一次，各保护格在同一张残量网上
    分别校验（不同保护格允许使用不同的可能表）。
    """
    key = frozenset((k, s, f) for (k, s, f, _v) in fact_set)
    cached = cache.get(key)
    if cached is not None:
        return cached
    flow = MarginFlow(stations, fleets, upper, sorted(fact_set))
    if not flow.feasible():
        cache[key] = {"safe": False, "violations": {None: ["infeasible"]}}
        return cache[key]
    violations = {}
    for cell in protected:
        s, f, d = cell["station_id"], cell["fleet_id"], cell["margin_wh"]
        i, j = s_index[s], f_index[f]
        true_v = grid.get((s, f), 0)
        margin = flow.margins(i, j, true_v, d)
        failed = []
        if not margin["low_ok"]:
            failed.append("low")
        if not margin["high_ok"]:
            failed.append("high")
        if failed:
            violations[(s, f)] = failed
    result = {"safe": not violations, "violations": violations}
    cache[key] = result
    return result


def _search_best(packages, history_facts, upper, stations, fleets, protected,
                 grid, s_index, f_index):
    """精确分支限界：价值最高；并列取排序后标识序列字典序最小者。

    可行性关于事实集合单调（发布更多事实只会收紧可行域），据此记忆化与
    剪枝；绝不逐包独立审批，也不按价值贪心。
    """
    m = len(packages)
    ordered = sorted(packages, key=lambda p: (-p["value"], p["package_id"]))
    suffix_value = [0] * (m + 1)
    for i in range(m - 1, -1, -1):
        suffix_value[i] = suffix_value[i + 1] + ordered[i]["value"]

    cache = {}
    history_set = frozenset(history_facts)

    def safe(fact_tuples):
        return _evaluate_fact_set(
            set(fact_tuples), upper, stations, fleets, protected, grid,
            s_index, f_index, cache)["safe"]

    best = {"value": -1, "ids": None}

    def dfs(i, chosen_ids, chosen_facts, value):
        if value + suffix_value[i] < best["value"]:
            return
        if i == m:
            ids = tuple(sorted(chosen_ids))
            if value > best["value"] or (
                    value == best["value"]
                    and (best["ids"] is None or ids < best["ids"])):
                if safe(history_set | frozenset(chosen_facts)):
                    best.update(value=value, ids=ids)
            return
        pack = ordered[i]
        # 先尝试纳入，再尝试排除（ordered 已按价值降序，利于尽早拿优解）。
        dfs(i + 1, chosen_ids + [pack["package_id"]],
            chosen_facts + pack["fact_tuples"], value + pack["value"])
        dfs(i + 1, chosen_ids, chosen_facts, value)

    dfs(0, [], [], 0)
    return best["ids"] if best["ids"] is not None else ()


# ---------------------------------------------------------------------------
# 5) 主入口
# ---------------------------------------------------------------------------

def compile_report(state, request):
    """从闭账 state 与发布请求编制本轮可发布报表与内部审核说明（纯函数）。

    :raises ReportingError: ``invalid_input`` / ``conflicting_history`` /
        ``prior_risk``，失败时不产生任何部分结果、不写回 state。
    :return: ``(public_report, internal_review)`` 两个可直接 JSON 序列化的字典。
    """
    (period_id, fleet_map, stations, fleets,
     station_set, fleet_set, upper) = _parse_request(request)

    grid = _aggregate_closed_period(state, period_id, fleet_map)

    # 内部汇总不得越过公开上限
    over = sorted((s, f, w) for (s, f), w in grid.items()
                  if w > upper.get((s, f), -1))
    _require(not over, f"本期真实汇总越过公开上限: {over}")
    # 真实用量涉及的站点/车队必须在公开口径内
    for s, f in grid:
        _require(s in station_set and f in fleet_set,
                 f"本期汇总坐标 ({s!r},{f!r}) 不在公开口径内（未知坐标）")

    history_facts = _parse_history(request, station_set, fleet_set, upper, grid)
    packages = _parse_packages(request, station_set, fleet_set)
    protected = _parse_protected(request, station_set, fleet_set)

    s_index = {s: i for i, s in enumerate(stations)}
    f_index = {f: j for j, f in enumerate(fleets)}

    # 候选包 -> 带内部汇总值的事实元组
    for pack in packages:
        pack["fact_tuples"] = [
            _build_fact(_normalize_fact(item, "request", station_set, fleet_set)[0],
                        grid, stations, fleets)
            for item in pack["requests"]]

    # 已有风险：仅凭上限 + 历史事实，保密余量就已不成立（可区分错误，
    # 不能当作正常的空发布）。
    cache = {}
    prior = _evaluate_fact_set(set(history_facts), upper, stations, fleets,
                               protected, grid, s_index, f_index, cache)
    if not prior["safe"]:
        details = "; ".join(
            f"({s},{f}) 失去 {('/'.join(side))} 侧余量"
            for (s, f), side in sorted(prior["violations"].items())
            if isinstance(s, str))
        raise ReportingError(
            "prior_risk",
            "仅用公开上限与历史已公开数值即无法满足保密余量要求"
            + (f"：{details}" if details else "（公开事实系统无可行整数表）"))

    selected_ids = _search_best(packages, history_facts, upper, stations, fleets,
                                protected, grid, s_index, f_index)
    selected = [p for p in packages if p["package_id"] in set(selected_ids)]
    selected.sort(key=lambda p: p["package_id"])

    # ---- 对外 JSON：周期、选中的包及其发布数值 ----
    public_packages = []
    for pack in selected:
        values = []
        for fact in sorted(pack["fact_tuples"]):
            kind, s, f, v = fact
            values.append({"fact_type": kind, "station_id": s,
                           "fleet_id": f, "value_wh": v})
        public_packages.append({"package_id": pack["package_id"],
                                "published_facts": values})
    public_report = {
        "report_version": REPORT_VERSION,
        "period_id": period_id,
        "selected_packages": public_packages,
    }

    # ---- 内部审核 JSON：选中标识、总价值、每个未选包的加入后果 ----
    base_facts = frozenset(history_facts) | frozenset(
        fact for pack in selected for fact in pack["fact_tuples"])
    rejected = []
    for pack in sorted(packages, key=lambda p: p["package_id"]):
        if pack["package_id"] in set(selected_ids):
            continue
        evaluation = _evaluate_fact_set(
            set(base_facts | frozenset(pack["fact_tuples"])),
            upper, stations, fleets, protected, grid,
            s_index, f_index, cache)
        if evaluation["safe"]:
            rejected.append({"package_id": pack["package_id"],
                             "would_violate": [],
                             "reason": "not_in_best_combination"})
        else:
            would = []
            for (s, f), sides in sorted(evaluation["violations"].items()):
                if not isinstance(s, str):
                    continue  # 系统级不可行不对应具体保护格
                for side in sides:
                    would.append({"station_id": s, "fleet_id": f,
                                  "failed_side": side})
            would.sort(key=lambda x: (x["station_id"], x["fleet_id"],
                                      x["failed_side"]))
            rejected.append({"package_id": pack["package_id"],
                             "would_violate": would,
                             "reason": "secrecy_violation"})
    internal_review = {
        "report_version": REPORT_VERSION,
        "period_id": period_id,
        "selected_package_ids": list(selected_ids),
        "total_value": sum(p["value"] for p in selected),
        "protected_cells": [dict(c) for c in sorted(
            protected, key=lambda c: (c["station_id"], c["fleet_id"]))],
        "rejected_packages": rejected,
    }
    return public_report, internal_review
