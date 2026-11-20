"""带下界整数循环流：公开事实下的精确可行性与保密余量校验。

保密口径中的「可能用量表」是一张站点×车队整数矩阵，约束只有：

* 每格 ``0 <= x_{i,j} <= U_{i,j}``（公开非负整数上限，不可能服务的组合
  上限为零）；
* 精确事实：某格定值、某站点行小计定值、某车队列小计定值、全表总计定值。

建模为带边下界的容量网络并做标准的「固定下界 + 超级源/汇」循环转换：

    SRC -> 站点 i -> 车队 j -> SINK，再加回边 SINK -> SRC
    （有全表总计时回边固定为该总计值；否则回边容量取上限总和）

每条边下界为 0，唯独自 ``cell`` 事实来的格边把下界与上界都固定为该精确
值（``lo = hi = v``）。在超级源/汇上跑一次整数 Dinic 最大流，当且仅当
全部超级源边饱和时存在满足所有事实的整数循环，即一张整数可能用量表；
再对原始边逐条复核节点守恒，杜绝超级边残量干扰。整数容量天然给出整数解。

保密余量校验用**带界改界重算**而不是枚举 Wh：问某格能否取到不超过
``v-d``，就把该格边限制在 ``[0, v-d]`` 再跑一次可行性；问能否取到至少
``v+d`` 就限制在 ``[v+d, U]``。“限制在 ``[0, t]`` 仍可行”关于 ``t``
单调，因此极值也可用 ``O(log U)`` 次可行性判定二分得到。无论周期电量多大，
每次判定只做图运算，绝不逐格枚举 Wh。

标准库实现、整数精确、确定性：边按固定顺序加入，BFS/DFS 按邻接表顺序
探索，结果与输入记录次序无关。
"""


class _Dinic:
    """邻接表残量网络上的 Dinic 整数最大流（确定性，边为 ``[to, cap, rev]``）。"""

    def __init__(self, n):
        self.n = n
        self.graph = [[] for _ in range(n)]

    def add_edge(self, u, v, cap):
        self.graph[u].append([v, cap, len(self.graph[v])])
        self.graph[v].append([u, 0, len(self.graph[u]) - 1])

    def max_flow(self, source, sink, limit=None):
        flow = 0
        n = self.n
        hard_limit = float("inf") if limit is None else int(limit)
        while flow < hard_limit:
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

            while flow < hard_limit:
                add = dfs(source, hard_limit - flow)
                if not add:
                    break
                flow += add
        return flow


class MarginFlow:
    """对给定公开上限与精确事实集合做循环流可行性与格子余量分析。

    事实以内部规范化元组给出（见 :mod:`charging.reporting.engine`）：

    * ``("cell", s, f, v)``        格子 (s,f) 恰好为 v；
    * ``("station", s, None, v)``  站点 s 的行小计恰好为 v；
    * ``("fleet", None, f, v)``    车队 f 的列小计恰好为 v；
    * ``("total", None, None, v)`` 全表总计恰好为 v。
    """

    def __init__(self, stations, fleets, upper, facts):
        self.stations = list(stations)
        self.fleets = list(fleets)
        self.n_s = len(self.stations)
        self.n_f = len(self.fleets)
        self.upper = upper
        self.facts = list(facts)
        self.s_idx = {s: i for i, s in enumerate(self.stations)}
        self.f_idx = {f: j for j, f in enumerate(self.fleets)}

    # ------------------------------------------------------------------
    def _build_network(self, bound=None):
        """构造并求解一次循环流。

        ``bound=(s,f,lo_t,hi_t)`` 时把该格的取值限制在 ``[lo_t, hi_t]``；
        普通 pin（恰好 t）即传 ``(s,f,t,t)``。返回 True 当且仅当存在满足全部
        事实（含可选界）的整数表。
        """
        n_s, n_f = self.n_s, self.n_f
        lower = [[0] * n_f for _ in range(n_s)]
        cap = [[int(self.upper[(self.stations[i], self.fleets[j])])
                for j in range(n_f)] for i in range(n_s)]
        row_exact, col_exact, grand = {}, {}, None
        for kind, s, f, v in self.facts:
            if kind == "cell":
                lower[self.s_idx[s]][self.f_idx[f]] = int(v)
                cap[self.s_idx[s]][self.f_idx[f]] = int(v)
            elif kind == "station":
                row_exact[self.s_idx[s]] = int(v)
            elif kind == "fleet":
                col_exact[self.f_idx[f]] = int(v)
            else:
                grand = int(v)
        if bound is not None:
            s, f, lo_t, hi_t = bound
            i, j = self.s_idx[s], self.f_idx[f]
            lo_t, hi_t = int(lo_t), int(hi_t)
            if hi_t < lower[i][j] or lo_t > cap[i][j] or lo_t > hi_t:
                return False
            # 与既有 cell 精确下界取较大者、上界取较小者
            lower[i][j] = max(lower[i][j], lo_t)
            cap[i][j] = min(cap[i][j], hi_t)
        for i in range(n_s):
            for j in range(n_f):
                if lower[i][j] > cap[i][j]:
                    return False

        # 节点：SRC 0，站点 1..S，车队 1+S..S+F，SINK 1+S+F，超源 SS，超汇 TT
        src = 0
        sink = 1 + n_s + n_f
        ss = sink + 1
        tt = ss + 1
        n_nodes = tt + 1
        dinic = _Dinic(n_nodes)
        demand = [0] * n_nodes
        edges = []  # (u, v, lo, hi, edge_index)

        def add_lb(u, v, lo, hi):
            if hi - lo < 0:
                return False
            idx = len(dinic.graph[u])
            dinic.add_edge(u, v, hi - lo)
            demand[u] -= lo
            demand[v] += lo
            edges.append((u, v, lo, hi, idx))
            return True

        total_cap = sum(sum(row) for row in cap)
        for i in range(n_s):
            u = 1 + i
            for j in range(n_f):
                if not add_lb(u, 1 + n_s + j, lower[i][j], cap[i][j]):
                    return False
        for i in range(n_s):
            if i in row_exact:
                if not add_lb(src, 1 + i, row_exact[i], row_exact[i]):
                    return False
            else:
                add_lb(src, 1 + i, 0, total_cap)
        for j in range(n_f):
            u = 1 + n_s + j
            if j in col_exact:
                if not add_lb(u, sink, col_exact[j], col_exact[j]):
                    return False
            else:
                add_lb(u, sink, 0, total_cap)
        if grand is not None:
            if not add_lb(sink, src, grand, grand):
                return False
        else:
            add_lb(sink, src, 0, total_cap)

        required = 0
        for node in range(n_nodes):
            d = demand[node]
            if d > 0:
                dinic.add_edge(ss, node, d)
                required += d
            elif d < 0:
                dinic.add_edge(node, tt, -d)
        if dinic.max_flow(ss, tt) != required:
            return False

        # 复核：超级边饱和后，原始边必须构成节点守恒的可行循环。
        balance = [0] * n_nodes
        for u, v, lo, hi, idx in edges:
            used = dinic.graph[v][dinic.graph[u][idx][2]][1]
            if not (0 <= used <= hi - lo):
                return False
            value = lo + used
            balance[u] -= value
            balance[v] += value
        if any(balance[node] != 0 for node in range(sink + 1)):
            return False
        return True

    def feasible(self):
        """约束系统是否存在整数可行表。"""
        return self._build_network()

    def _cell_feasible_with_bound(self, i, j, lo_t, hi_t):
        station, fleet = self.stations[i], self.fleets[j]
        return self._build_network(bound=(station, fleet, lo_t, hi_t))

    def margins(self, i, j, true_value, d):
        """该格是否同时存在 ``x <= v-d`` 与 ``x >= v+d`` 的可行整数表。

        返回 ``{"low_ok": bool, "high_ok": bool}``。用带界改界的整数流精确
        判定：低侧把该格限制在 ``[0, v-d]`` 看是否仍可行，高侧限制在
        ``[v+d, U]``。不同保护格可分别使用不同的可能表，不枚举任何 Wh。
        """
        station, fleet = self.stations[i], self.fleets[j]
        upper = int(self.upper[(station, fleet)])
        low_target = int(true_value) - int(d)
        high_target = int(true_value) + int(d)
        low_ok = (low_target >= 0
                  and self._cell_feasible_with_bound(i, j, 0, low_target))
        high_ok = (high_target <= upper
                   and self._cell_feasible_with_bound(i, j, high_target, upper))
        return {"low_ok": low_ok, "high_ok": high_ok}

    def extremum(self, i, j):
        """该格在全部公开事实下的精确整数 ``(最小值, 最大值)``。

        “把该格限制在 ``[0, t]`` 仍可行”关于 ``t`` 单调，二分求最小可行
        上界即最小值；对称地用 ``[t, U]`` 求最大值。每次判定都是一次整数
        循环流，共 ``O(log U)`` 次，不枚举 Wh。仅供测试/审计使用。
        """
        upper = int(self.upper[(self.stations[i], self.fleets[j])])
        # 最小值：最小的 t 使 [0, t] 可行
        lo, hi = 0, upper
        while lo < hi:
            mid = (lo + hi) // 2
            if self._cell_feasible_with_bound(i, j, 0, mid):
                hi = mid
            else:
                lo = mid + 1
        minimum = lo
        # 最大值：最大的 t 使 [t, U] 可行
        lo, hi = 0, upper
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._cell_feasible_with_bound(i, j, mid, upper):
                lo = mid
            else:
                hi = mid - 1
        return minimum, lo
