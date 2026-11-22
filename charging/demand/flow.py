"""带上下界的整数循环流：需量复核的精确可行性求解器。

计量证据的可行解释天然是一张容量网络上的整数循环流（详见
``docs/demand.md`` 第 4 节）：

    源 -> 站点读数区间 -> 刻钟（或合并块）-> 上游读数区间 -> 汇 -> 回边到源

每条边带整数下界与上界，用标准的「固定下界 + 超级源/汇」转换后在整数容量
上跑一次 Dinic 最大流判定可行性；整数容量保证找到的可行流本身就是每站每
刻钟的整数 Wh 解释。无论累计电量是几百 Wh 还是数百万 Wh，每次判定都只做
图运算，**绝不逐 Wh 枚举**，也不枚举逐刻钟分摊方案。

边按固定顺序加入、BFS/DFS 按邻接表顺序探索，结果确定且与输入记录次序无关。
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


class Circulation:
    """带边下界 ``[lo, hi]`` 的整数循环流可行性问题。

    ``add_edge`` 按调用顺序登记边；``solve`` 返回每条边的整数流量列表
    （与登记顺序一致），无可行循环流时返回 ``None``。
    """

    def __init__(self, num_nodes):
        self.num_nodes = num_nodes
        self.edges = []  # (u, v, lo, hi)

    def add_edge(self, u, v, lo, hi):
        self.edges.append((u, v, int(lo), int(hi)))
        return len(self.edges) - 1

    def solve(self):
        """存在满足全部边界的整数循环流时返回各边流量，否则 ``None``。"""
        n = self.num_nodes
        demand = [0] * n
        dinic = _Dinic(n + 2)
        arc_index = []
        for u, v, lo, hi in self.edges:
            if hi < lo or lo < 0:
                return None
            arc_index.append(len(dinic.graph[u]))
            dinic.add_edge(u, v, hi - lo)
            demand[u] -= lo
            demand[v] += lo
        if sum(demand) != 0:  # 供需总量必须平衡，否则不存在可行循环流
            return None
        ss, tt = n, n + 1
        required = 0
        for node in range(n):
            d = demand[node]
            if d > 0:
                dinic.add_edge(ss, node, d)
                required += d
            elif d < 0:
                dinic.add_edge(node, tt, -d)
        if dinic.max_flow(ss, tt) != required:
            return None
        flows = []
        for i, (u, v, lo, hi) in enumerate(self.edges):
            idx = arc_index[i]
            used = dinic.graph[v][dinic.graph[u][idx][2]][1]
            flows.append(lo + used)
        # 复核：原始边流量必须构成节点守恒的可行循环，杜绝超级边残量干扰。
        balance = [0] * n
        for (u, v, lo, hi), f in zip(self.edges, flows):
            if not (lo <= f <= hi):
                return None
            balance[u] -= f
            balance[v] += f
        if any(balance):
            return None
        return flows
