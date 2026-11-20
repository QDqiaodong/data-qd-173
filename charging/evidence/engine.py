"""充电启动证据核验：请求日志与桩端结果日志的最优关联（纯函数、无副作用）。

完整契约见 ``docs/evidence.md``。一次处理同一站点的一份日志快照（两份日志：
手机端/现场终端的**请求**日志 + 桩端的**结果**日志），交付两份产物：

* **客服证据清单**：在每份最优解释中都出现的**确证配对**、最优解释中始终
  没有配对的记录、以及其余有待人工核查的记录。清单只陈述这批证据支持的
  关联；未配对留空不生成任何桩端故障事实。
* **运营报告**：半开请求时间窗内，站点整体与两个渠道各自的成功率区间
  （所有最优解释中可达到的最小/最大成功数）与目标基点值的整数精确比较，
  每个端点附一份可核查的完整配对作为证据。

关联口径：

* 同枪口、结果时刻不早于请求时刻且落后不超过 120 秒（含边界），两条记录
  才可能关联；两侧关联码都非空且不相同则不能关联。
* 两侧关联码相同且非空是强证据；仅一侧带码或两侧都无码仅是普通时间证据。
* 一次解释中每条请求、每条结果最多配对一次，允许留下未配对项。优先最大
  化强证据配对数，再在此基础上最大化配对总数；不按时间更近、记录名称或
  输入顺序打破业务上的平手。

求解口径（精确，不枚举全部解释）：

* 每个连通分量上用**最小费用最大流**求字典序最优（强证据数、配对总数），
  权重为整数，结果精确。
* “某配对是否出现在每份/某份最优解释中”由最优流的**零既约费用残量图**的
  强连通分量判定（对偶最优性：另一最优解 ⟺ 原最优流叠加零费用环流），
  与具体参考解、势函数选取无关。
* 每组成功数的最小/最大值用第二层整数权重在同一网络上精确求解；不同组、
  不同端点可以来自不同解释。

本模块与计量结算（``charging.engine``）、收款准入（``charging.workbench``）
和未来排程（``charging.planning``）完全解耦：只读两份日志，不读写它们的
任何状态，也不把许可启动、桩端故障或计划量写入既有状态。
"""

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from heapq import heappop, heappush

EVIDENCE_VERSION = 1

# 结果时刻允许落后于请求时刻的最大秒数（含边界）。
MAX_RESULT_LAG_SECONDS = 120

CHANNELS = ("mobile", "terminal")   # mobile=手机端, terminal=现场终端
OUTCOMES = ("started", "failed")    # started=实际启动, failed=启动故障

# 运营报告的三组口径：站点整体 + 两个渠道。
GROUPS = ("station", "mobile", "terminal")

# 配对证据类型。
EVIDENCE_STRONG = "strong"   # 两侧关联码相同且非空
EVIDENCE_TIME = "time"       # 仅时间窗口证据（一侧带码或两侧都无码）

# 组达标状态。
STATUS_MET = "met"                 # 最低成功率也达到目标：确认达标
STATUS_NOT_MET = "not_met"         # 最高成功率仍低于目标：确认未达标
STATUS_UNDETERMINED = "undetermined"  # 证据不足以判定
STATUS_NO_DATA = "no_data"         # 组内窗口无请求：无数据，不声称达标


class EvidenceError(ValueError):
    """输入未通过校验。

    ``code`` 为稳定的机器可读错误码（CLI 退出码 2 时打印），``message``
    为人类可读说明。本模块只有一类失败：``invalid_input``（结构、字段、
    站点口径、时刻、重复身份、窗口或目标基点非法）。
    """

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _require(cond, message):
    if not cond:
        raise EvidenceError("invalid_input", message)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value, what):
    _require(isinstance(value, dict), f"{what} 必须是 JSON 对象")


def _require_id(value, what):
    _require(isinstance(value, str) and value.strip() != "",
             f"{what} 必须是非空（非空白）字符串")


def _check_known_keys(obj, allowed, what):
    extra = sorted(set(obj) - set(allowed))
    _require(not extra, f"{what} 含未知字段: {extra}")


def _parse_instant(value, what):
    """要求带显式偏移、精确到秒的 ISO 时间；按实际瞬时比较。"""
    _require(isinstance(value, str) and value != "",
             f"{what} 必须是带显式偏移的 ISO 时间字符串")
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("invalid_input", f"{what} 无法解析: {value!r}") from exc
    _require(d.tzinfo is not None and d.utcoffset() is not None,
             f"{what} 必须带显式偏移: {value!r}")
    _require(d.microsecond == 0, f"{what} 必须精确到秒（不允许亚秒部分）: {value!r}")
    return d


def _iso(dt):
    """统一规范化为 UTC ISO 表示，保证输出与输入写法无关。"""
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 输入解析与校验
# ---------------------------------------------------------------------------

_REQUEST_REQUIRED = ("request_id", "gun_id", "requested_at", "channel")
_REQUEST_OPTIONAL = ("correlation_code",)
_RESULT_REQUIRED = ("result_id", "gun_id", "resolved_at", "outcome")
_RESULT_OPTIONAL = ("correlation_code",)
_REQUESTS_TOP_KEYS = ("station_id", "observed_until", "requests")
_RESULTS_TOP_KEYS = ("station_id", "observed_until", "results")


def _parse_code(rec, what):
    code = rec.get("correlation_code")
    _require(code is None or isinstance(code, str),
             f"{what}.correlation_code 必须是字符串或 null")
    # 空串与缺省同义：都不是“非空关联码”
    return code if code else None


def _parse_requests(raw, cutoff):
    _require(isinstance(raw, list), "requests 必须是数组")
    reqs = []
    seen = set()
    for idx, rec in enumerate(raw):
        what = f"requests[{idx}]"
        _require_object(rec, what)
        _check_known_keys(rec, _REQUEST_REQUIRED + _REQUEST_OPTIONAL, what)
        for key in _REQUEST_REQUIRED:
            _require(key in rec, f"{what} 缺少字段 {key}")
        rid = rec["request_id"]
        _require_id(rid, f"{what}.request_id")
        _require(rid not in seen, f"request_id 重复: {rid!r}")
        seen.add(rid)
        _require_id(rec["gun_id"], f"{what}.gun_id")
        at = _parse_instant(rec["requested_at"], f"{what}.requested_at")
        _require(at <= cutoff, f"{what}.requested_at 晚于观测截止 observed_until: {rec['requested_at']!r}")
        channel = rec["channel"]
        _require(channel in CHANNELS,
                 f"{what}.channel 必须是 {list(CHANNELS)} 之一: {channel!r}")
        reqs.append({
            "request_id": rid,
            "gun_id": rec["gun_id"],
            "at": at,
            "channel": channel,
            "code": _parse_code(rec, what),
            "in_window": False,
        })
    return reqs


def _parse_results(raw, cutoff):
    _require(isinstance(raw, list), "results 必须是数组")
    results = []
    seen = set()
    for idx, rec in enumerate(raw):
        what = f"results[{idx}]"
        _require_object(rec, what)
        _check_known_keys(rec, _RESULT_REQUIRED + _RESULT_OPTIONAL, what)
        for key in _RESULT_REQUIRED:
            _require(key in rec, f"{what} 缺少字段 {key}")
        rid = rec["result_id"]
        _require_id(rid, f"{what}.result_id")
        _require(rid not in seen, f"result_id 重复: {rid!r}")
        seen.add(rid)
        _require_id(rec["gun_id"], f"{what}.gun_id")
        at = _parse_instant(rec["resolved_at"], f"{what}.resolved_at")
        _require(at <= cutoff, f"{what}.resolved_at 晚于观测截止 observed_until: {rec['resolved_at']!r}")
        outcome = rec["outcome"]
        _require(outcome in OUTCOMES,
                 f"{what}.outcome 必须是 {list(OUTCOMES)} 之一: {outcome!r}")
        results.append({
            "result_id": rid,
            "gun_id": rec["gun_id"],
            "at": at,
            "outcome": outcome,
            "code": _parse_code(rec, what),
        })
    return results


def _parse_snapshot(requests_doc, results_doc):
    """解析并校验两份日志；返回 (station_id, 观测截止, 请求列表, 结果列表)。"""
    _require_object(requests_doc, "请求日志")
    _check_known_keys(requests_doc, _REQUESTS_TOP_KEYS, "请求日志")
    for key in _REQUESTS_TOP_KEYS:
        _require(key in requests_doc, f"请求日志缺少字段 {key}")
    _require_object(results_doc, "桩端日志")
    _check_known_keys(results_doc, _RESULTS_TOP_KEYS, "桩端日志")
    for key in _RESULTS_TOP_KEYS:
        _require(key in results_doc, f"桩端日志缺少字段 {key}")

    station = requests_doc["station_id"]
    _require_id(station, "请求日志.station_id")
    _require(results_doc["station_id"] == station,
             "两份日志的 station_id 必须一致（同一站点快照）")

    cutoff_req = _parse_instant(requests_doc["observed_until"], "请求日志.observed_until")
    cutoff_res = _parse_instant(results_doc["observed_until"], "桩端日志.observed_until")
    _require(cutoff_req == cutoff_res,
             "两份日志的 observed_until 必须表示同一观测截止时刻")

    reqs = _parse_requests(requests_doc["requests"], cutoff_req)
    results = _parse_results(results_doc["results"], cutoff_req)
    return station, cutoff_req, reqs, results


# ---------------------------------------------------------------------------
# 关联候选与连通分量
# ---------------------------------------------------------------------------

def _compatible_edges(reqs, results):
    """全部可能关联的 (请求下标, 结果下标, 是否强证据)，按 (请求, 结果) 排序。"""
    by_gun = {}
    for si, res in enumerate(results):
        by_gun.setdefault(res["gun_id"], []).append(si)
    gun_times = {}
    for gun, sis in by_gun.items():
        sis.sort(key=lambda si: (results[si]["at"], results[si]["result_id"]))
        gun_times[gun] = [results[si]["at"] for si in sis]

    lag = timedelta(seconds=MAX_RESULT_LAG_SECONDS)
    edges = []
    for ri, req in enumerate(reqs):
        sis = by_gun.get(req["gun_id"])
        if not sis:
            continue
        times = gun_times[req["gun_id"]]
        lo = bisect_left(times, req["at"])
        hi = bisect_right(times, req["at"] + lag)
        for si in sis[lo:hi]:
            res = results[si]
            if req["code"] and res["code"] and req["code"] != res["code"]:
                continue  # 两侧关联码都非空且不相同：不能关联
            strong = bool(req["code"]) and bool(res["code"])
            edges.append((ri, si, strong))
    edges.sort(key=lambda e: (e[0], e[1]))
    return edges


def _components(n_req, n_res, edges):
    """按关联候选把记录分成连通分量；只返回至少含一条候选边的分量。

    分量内元素按下标（即标识字典序）排列，边按 (请求, 结果) 排列，
    保证后续求解与输入行序无关。
    """
    parent = list(range(n_req + n_res))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ri, si, _ in edges:
        ra, rb = find(ri), find(n_req + si)
        if ra != rb:
            parent[ra] = rb

    groups = {}
    for ri, si, strong in edges:
        groups.setdefault(find(ri), {"reqs": set(), "results": set(), "edges": []})
        g = groups[find(ri)]
        g["reqs"].add(ri)
        g["results"].add(si)
        g["edges"].append((ri, si, strong))

    comps = []
    for g in groups.values():
        req_list = sorted(g["reqs"])
        res_list = sorted(g["results"])
        ri_map = {ri: k for k, ri in enumerate(req_list)}
        si_map = {si: k for k, si in enumerate(res_list)}
        local_edges = sorted(
            (ri_map[ri], si_map[si], strong) for ri, si, strong in g["edges"])
        comps.append({"reqs": req_list, "results": res_list, "edges": local_edges})
    comps.sort(key=lambda c: (c["reqs"][0], c["results"][0]))
    return comps


# ---------------------------------------------------------------------------
# 最小费用最大流（单位容量、整数费用、确定性）
# ---------------------------------------------------------------------------

class _MinCostFlow:
    """邻接表残量网络上的连续最短路（势函数 + Dijkstra），全整数费用。

    边按固定顺序加入，堆元素为 (距离, 节点编号) 二元组，平局按节点编号
    裁决，因此对同一张图总是得到同一个最优流，保证输出确定性。
    """

    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]
        self.potential = [0] * n

    def add_edge(self, u, v, cap, cost):
        self.g[u].append([v, cap, cost, len(self.g[v])])
        self.g[v].append([u, 0, -cost, len(self.g[u]) - 1])

    def solve(self, source, sink):
        """在允许任意流量下求最小费用流（仅当最短路费用为负才增广）。"""
        n = self.n
        g = self.g
        pot = self.potential
        # 初始势：Bellman-Ford（初始残量图无负环：费用为负的边只从请求侧
        # 指向结果侧，图是二部无环的）。
        for _ in range(n - 1):
            updated = False
            for u in range(n):
                du = pot[u]
                for e in g[u]:
                    if e[1] > 0 and du + e[2] < pot[e[0]]:
                        pot[e[0]] = du + e[2]
                        updated = True
            if not updated:
                break
        inf = float("inf")
        while True:
            dist = [inf] * n
            dist[source] = 0
            prev_v = [-1] * n
            prev_e = [-1] * n
            pq = [(0, source)]
            while pq:
                d, u = heappop(pq)
                if dist[u] < d:
                    continue
                for i, e in enumerate(g[u]):
                    if e[1] > 0:
                        nd = d + e[2] + pot[u] - pot[e[0]]
                        if nd < dist[e[0]]:
                            dist[e[0]] = nd
                            prev_v[e[0]] = u
                            prev_e[e[0]] = i
                            heappush(pq, (nd, e[0]))
            if dist[sink] == inf:
                return
            actual = dist[sink] + pot[sink] - pot[source]
            if actual >= 0:
                return  # 再增广只会变差（或不变）：当前流即字典序最优
            for v in range(n):
                if dist[v] < inf:
                    pot[v] += dist[v]
            # 瓶颈容量（源->请求边容量为 1，故恒为 1；仍按通用计算）
            d = inf
            v = sink
            while v != source:
                e = g[prev_v[v]][prev_e[v]]
                d = min(d, e[1])
                v = prev_v[v]
            v = sink
            while v != source:
                e = g[prev_v[v]][prev_e[v]]
                e[1] -= d
                g[v][e[3]][1] += d
                v = prev_v[v]


def _tarjan_scc(n, adj):
    """迭代版 Tarjan 强连通分量；返回每个节点的分量编号（确定性）。"""
    index = [0] * n      # 0 表示未访问；发现序号从 1 开始
    low = [0] * n
    on_stack = [False] * n
    comp = [-1] * n
    stack = []
    counter = 1
    ncomp = 0
    for root in range(n):
        if index[root]:
            continue
        work = [(root, 0)]
        while work:
            v, ci = work[-1]
            if ci == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on_stack[v] = True
            if ci < len(adj[v]):
                work[-1] = (v, ci + 1)
                w = adj[v][ci]
                if index[w] == 0:
                    work.append((w, 0))
                elif on_stack[w] and index[w] < low[v]:
                    low[v] = index[w]
            else:
                work.pop()
                if low[v] == index[v]:
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp[w] = ncomp
                        if w == v:
                            break
                    ncomp += 1
                if work:
                    u = work[-1][0]
                    if low[v] < low[u]:
                        low[u] = low[v]
    return comp


def _build_and_solve(n_req, n_res, edges, weight_of):
    """建流网络并求解；返回 (流, 配对的 (lri, lsi) 列表, 中间边索引)。"""
    source = 0
    res_base = 1 + n_req
    sink = 1 + n_req + n_res
    mf = _MinCostFlow(sink + 1)
    for ri in range(n_req):
        mf.add_edge(source, 1 + ri, 1, 0)
    mid = {}
    for lri, lsi, strong in edges:
        mf.add_edge(1 + lri, res_base + lsi, 1, -weight_of(lri, lsi, strong))
        mid[(lri, lsi)] = len(mf.g[1 + lri]) - 1
    for lsi in range(n_res):
        mf.add_edge(res_base + lsi, sink, 1, 0)
    mf.solve(source, sink)
    pairs = [(lri, lsi) for (lri, lsi), ei in mid.items()
             if mf.g[1 + lri][ei][1] == 0]
    return mf, pairs, mid


# ---------------------------------------------------------------------------
# 分量级求解：参考最优解 + 全部最优解的成员资格判定
# ---------------------------------------------------------------------------

def _primary(comp, big):
    """分量字典序最优（强证据数、配对总数）参考解与成员资格判定。

    返回 ``(pairs, in_all, req_possible, res_possible)``：

    * ``pairs``：参考最优配对的 ``(lri, lsi)`` 列表；
    * ``in_all``：出现在**每份**最优解释中的配对集合（``pairs`` 的子集）；
    * ``req_possible[lri]`` / ``res_possible[lsi]``：该记录是否出现在**某份**
      最优解释中（参考解中未配对的记录才有意义）。
    """
    n_req = len(comp["reqs"])
    n_res = len(comp["results"])
    edges = comp["edges"]
    mf, pairs, mid = _build_and_solve(
        n_req, n_res, edges, lambda lri, lsi, strong: big if strong else 1)

    # 对偶最优性：另一最优解 ⟺ 参考流叠加零既约费用环流。零既约费用残量
    # 边构成图 Z，一条残量边在某零费用环流上 ⟺ 其两端在 Z 的同一强连通
    # 分量中。该判定与参考解/势函数选取无关。
    pot = mf.potential
    n = mf.n
    adj = [[] for _ in range(n)]
    for u in range(n):
        for e in mf.g[u]:
            if e[1] > 0 and e[2] + pot[u] - pot[e[0]] == 0:
                adj[u].append(e[0])
    scc = _tarjan_scc(n, adj)

    source = 0
    sink = n - 1
    res_base = 1 + n_req
    # 配对 (lri, lsi) 在每份最优解释中 ⟺ 其反向残量边不在任何零费用环流上
    in_all = {(lri, lsi) for (lri, lsi) in pairs
              if scc[1 + lri] != scc[res_base + lsi]}

    # 参考解中未配对的请求：源->请求 残量边在某零费用环流上 ⟺ 它在某份
    # 最优解释中被配对（环流把它接入另一份最优配对）。
    req_possible = []
    for lri in range(n_req):
        fwd = mf.g[source][lri]
        if fwd[1] == 0:
            req_possible.append(True)  # 参考解中已配对
        else:
            rc = fwd[2] + pot[source] - pot[1 + lri]
            req_possible.append(rc == 0 and scc[1 + lri] == scc[source])
    res_possible = []
    for lsi in range(n_res):
        fwd = mf.g[res_base + lsi][-1]  # 结果->汇 边（该列表最后一条正向边）
        if fwd[1] == 0:
            res_possible.append(True)
        else:
            rc = fwd[2] + pot[res_base + lsi] - pot[sink]
            res_possible.append(rc == 0 and scc[res_base + lsi] == scc[sink])
    return pairs, in_all, req_possible, res_possible


def _secondary(comp, big, k, group, sense):
    """在最优解释集合内，精确求某组成功数的最大（sense=+1）或最小（sense=-1）。

    第二层权重 ``w1 * k ± bonus``：``k`` 大于任何解释的成功数上界，保证先
    满足第一层（强证据数、配对总数）字典序最优，再优化该组成功数。
    """
    reqs = comp["req_objs"]
    results = comp["res_objs"]

    def weight_of(lri, lsi, strong):
        w1 = big if strong else 1
        req = reqs[lri]
        bonus = 1 if (req["in_window"]
                      and (group == "station" or req["channel"] == group)
                      and results[lsi]["outcome"] == "started") else 0
        return w1 * k + sense * bonus

    _, pairs, _ = _build_and_solve(
        len(comp["reqs"]), len(comp["results"]), comp["edges"], weight_of)
    return pairs


# ---------------------------------------------------------------------------
# 输出视图
# ---------------------------------------------------------------------------

def _request_view(req):
    return {
        "request_id": req["request_id"],
        "gun_id": req["gun_id"],
        "channel": req["channel"],
        "requested_at": _iso(req["at"]),
        "correlation_code": req["code"],
    }


def _result_view(res):
    return {
        "result_id": res["result_id"],
        "gun_id": res["gun_id"],
        "outcome": res["outcome"],
        "resolved_at": _iso(res["at"]),
        "correlation_code": res["code"],
    }


def _pair_view(req, res, strong, in_window=None):
    view = {
        "request_id": req["request_id"],
        "result_id": res["result_id"],
        "gun_id": req["gun_id"],
        "channel": req["channel"],
        "outcome": res["outcome"],
        "evidence": EVIDENCE_STRONG if strong else EVIDENCE_TIME,
        "requested_at": _iso(req["at"]),
        "resolved_at": _iso(res["at"]),
    }
    if in_window is not None:
        view["in_window"] = in_window
    return view


def _success_count(pairs, reqs, results, pair_strong, group):
    """一组配对中“窗内该组请求被配到实际启动结果”的数量。"""
    return sum(
        1 for ri, si in pairs
        if reqs[ri]["in_window"]
        and (group == "station" or reqs[ri]["channel"] == group)
        and results[si]["outcome"] == "started")


def _witness_view(pairs, reqs, results, pair_strong, group):
    """一份可核查的完整配对（某最优解释），供端点取证。"""
    strong = sum(1 for p in pairs if pair_strong[p])
    views = [_pair_view(reqs[ri], results[si], pair_strong[(ri, si)],
                        in_window=reqs[ri]["in_window"])
             for ri, si in pairs]
    views.sort(key=lambda v: (v["request_id"], v["result_id"]))
    return {
        "strong_pairs": strong,
        "total_pairs": len(pairs),
        "successes": _success_count(pairs, reqs, results, pair_strong, group),
        "pairs": views,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def analyze(requests_doc, results_doc, window_start, window_end, target_bp):
    """核验一份站点日志快照，交付客服证据清单与运营报告（纯函数）。

    :param requests_doc: 请求日志（``station_id`` / ``observed_until`` /
        ``requests``，结构见 docs/evidence.md）。
    :param results_doc: 桩端日志（``station_id`` / ``observed_until`` /
        ``results``）。
    :param window_start: 半开请求时间窗起点（带显式偏移的 ISO 时间）。
    :param window_end: 半开请求时间窗终点；``observed_until`` 必须不早于
        ``window_end + 120 秒``。
    :param target_bp: 目标基点值，0 到 10000 的整数。
    :raises EvidenceError: 任何非法输入（``invalid_input``）。
    :return: 可直接 JSON 序列化的结果字典。
    """
    station, cutoff, reqs, results = _parse_snapshot(requests_doc, results_doc)

    ws = _parse_instant(window_start, "window_start")
    we = _parse_instant(window_end, "window_end")
    _require(ws < we, "window_start 必须早于 window_end（半开窗口非空）")
    _require(cutoff >= we + timedelta(seconds=MAX_RESULT_LAG_SECONDS),
             "observed_until 必须不早于 window_end + 120 秒，"
             "窗内请求才有完整的结果等待时间")
    _require(_is_int(target_bp) and 0 <= target_bp <= 10000,
             "target_bp 必须是 0 到 10000 的整数")

    # 记录按标识字典序排列：输入行序不影响任何结果。
    reqs = sorted(reqs, key=lambda r: r["request_id"])
    results = sorted(results, key=lambda r: r["result_id"])
    for req in reqs:
        req["in_window"] = ws <= req["at"] < we

    edges = _compatible_edges(reqs, results)
    pair_strong = {(ri, si): strong for ri, si, strong in edges}
    comps = _components(len(reqs), len(results), edges)
    for comp in comps:
        comp["req_objs"] = [reqs[ri] for ri in comp["reqs"]]
        comp["res_objs"] = [results[si] for si in comp["results"]]

    # 第一层权重：强证据 = big，普通 = 1；big 大于任何解释的配对总数，
    # 保证先最大化强证据数、再最大化配对总数。
    big = min(len(reqs), len(results)) + 1
    # 第二层权重基数：大于任何解释的组成功数上界。
    k = len(reqs) + 1

    # --- 主求解：确证配对 / 未配对 / 有待人工核查 -------------------------
    req_state = [None] * len(reqs)      # confirmed / review / unmatched
    res_state = [None] * len(results)
    confirmed = []                      # (ri, si)
    strong_pairs = 0
    total_pairs = 0
    for comp in comps:
        pairs, in_all, req_possible, res_possible = _primary(comp, big)
        strong_pairs += sum(1 for lri, lsi in pairs
                            if pair_strong[(comp["reqs"][lri], comp["results"][lsi])])
        total_pairs += len(pairs)
        for lri, lsi in pairs:
            ri, si = comp["reqs"][lri], comp["results"][lsi]
            if (lri, lsi) in in_all:
                confirmed.append((ri, si))
                req_state[ri] = "confirmed"
                res_state[si] = "confirmed"
            else:
                req_state[ri] = "review"
                res_state[si] = "review"
        matched_lri = {lri for lri, _ in pairs}
        matched_lsi = {lsi for _, lsi in pairs}
        for lri, ri in enumerate(comp["reqs"]):
            if lri not in matched_lri:
                req_state[ri] = "review" if req_possible[lri] else "unmatched"
        for lsi, si in enumerate(comp["results"]):
            if lsi not in matched_lsi:
                res_state[si] = "review" if res_possible[lsi] else "unmatched"
    # 不在任何分量中的记录（无关联候选）：最优解释中始终未配对。
    for ri, state in enumerate(req_state):
        if state is None:
            req_state[ri] = "unmatched"
    for si, state in enumerate(res_state):
        if state is None:
            res_state[si] = "unmatched"

    confirmed.sort(key=lambda p: (reqs[p[0]]["request_id"],
                                  results[p[1]]["result_id"]))

    # --- 运营报告：每组成功数区间 + 端点证据 ------------------------------
    groups_out = {}
    for group in GROUPS:
        denom = sum(1 for r in reqs
                    if r["in_window"]
                    and (group == "station" or r["channel"] == group))
        witnesses = {}
        endpoints = {}
        for sense, tag in ((-1, "min"), (+1, "max")):
            pairs = []
            for comp in comps:
                pairs.extend(
                    (comp["reqs"][lri], comp["results"][lsi])
                    for lri, lsi in _secondary(comp, big, k, group, sense))
            witnesses[tag] = _witness_view(pairs, reqs, results, pair_strong, group)
            endpoints[tag] = witnesses[tag]["successes"]
        lo, hi = endpoints["min"], endpoints["max"]
        if denom == 0:
            status = STATUS_NO_DATA
        elif lo * 10000 >= target_bp * denom:
            status = STATUS_MET
        elif hi * 10000 < target_bp * denom:
            status = STATUS_NOT_MET
        else:
            status = STATUS_UNDETERMINED
        groups_out[group] = {
            "requests_in_window": denom,
            "successes_min": lo,
            "successes_max": hi,
            "status": status,
            "witness_min": witnesses["min"],
            "witness_max": witnesses["max"],
        }

    return {
        "evidence_version": EVIDENCE_VERSION,
        "station_id": station,
        "observed_until": _iso(cutoff),
        "window": {"start": _iso(ws), "end": _iso(we)},
        "target_bp": target_bp,
        "matching_summary": {
            "requests": len(reqs),
            "results": len(results),
            "strong_pairs": strong_pairs,
            "total_pairs": total_pairs,
            "confirmed_pairs": len(confirmed),
        },
        "evidence": {
            "confirmed_pairs": [
                _pair_view(reqs[ri], results[si], pair_strong[(ri, si)])
                for ri, si in confirmed],
            "unmatched": {
                "requests": [_request_view(reqs[ri]) for ri in range(len(reqs))
                             if req_state[ri] == "unmatched"],
                "results": [_result_view(results[si]) for si in range(len(results))
                            if res_state[si] == "unmatched"],
            },
            "needs_review": {
                "requests": [_request_view(reqs[ri]) for ri in range(len(reqs))
                             if req_state[ri] == "review"],
                "results": [_result_view(results[si]) for si in range(len(results))
                            if res_state[si] == "review"],
            },
        },
        "operations": {
            "groups": groups_out,
        },
    }
