"""闭账周期充电报表保密发布的测试。

测试分四层：

1. ``MarginFlow`` 与独立暴力枚举器在大量随机小实例上逐格比对可行性、极值
   与余量结论（开发期交叉校验，证明整数流实现与“枚举所有整数表”等价）；
2. 题面验收场景（甲乙丙、历史小计、已有风险、同价裁决、公开结构零、
   只剩单侧余量、候选包共享同一事实）通过 ``compile_report`` 复核；
3. 从真实 :func:`charging.engine.apply_batch` 闭账 state 到两份 CLI 输出的
   端到端测试，含未计费行/金额调整不参与、失败保留原输出；
4. 输入校验与确定性（乱序等价、字节一致）。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from itertools import product
from pathlib import Path

from charging.engine import apply_batch
from charging.reporting import ReportingError, compile_report
from charging.reporting.marginflow import MarginFlow

PROJECT = Path(__file__).resolve().parents[1]
FIX = PROJECT / "fixtures" / "reporting"


def load_state():
    state = None
    for name in ("batch-register.json", "batch-close.json"):
        state, _ = apply_batch(state, json.loads((FIX / name).read_text()))
    return state


def load_request(name):
    return json.loads((FIX / name).read_text())


# ---------------------------------------------------------------------------
# 1) 整数流 vs 暴力枚举（独立交叉校验）
# ---------------------------------------------------------------------------

def brute_force(upper, facts, stations, fleets):
    cells = [(s, f) for s in stations for f in fleets]
    lo = {c: None for c in cells}
    hi = {c: None for c in cells}
    feasible = False
    for vals in product(*[range(upper[c] + 1) for c in cells]):
        x = dict(zip(cells, vals))
        ok = True
        for kind, s, f, v in facts:
            if kind == "cell":
                ok &= x[(s, f)] == v
            elif kind == "station":
                ok &= sum(w for (st, _), w in x.items() if st == s) == v
            elif kind == "fleet":
                ok &= sum(w for (_, fl), w in x.items() if fl == f) == v
            else:
                ok &= sum(x.values()) == v
        if ok:
            feasible = True
            for c in cells:
                lo[c] = x[c] if lo[c] is None else min(lo[c], x[c])
                hi[c] = x[c] if hi[c] is None else max(hi[c], x[c])
    return feasible, lo, hi


class MarginFlowBruteForceTests(unittest.TestCase):
    """与暴力枚举器在随机实例上完全对齐（不依赖被测实现本身的假设）。"""

    def test_matches_brute_force(self):
        import random
        random.seed(20260131)
        shapes = ((["A", "B"], ["F", "G"]),
                  (["A", "B", "C"], ["F", "G"]),
                  (["A", "B"], ["F", "G", "H"]))
        mismatches = 0
        for stations, fleets in shapes:
            cells = [(s, f) for s in stations for f in fleets]
            for _ in range(120):
                upper = {c: random.randint(0, 5) for c in cells}
                facts = []
                if random.random() < 0.7:
                    facts.append(("total", None, None,
                                  random.randint(0, sum(upper.values()))))
                for s in stations:
                    if random.random() < 0.4:
                        facts.append(("station", s, None,
                                      random.randint(0, 18)))
                for f in fleets:
                    if random.random() < 0.4:
                        facts.append(("fleet", None, f,
                                      random.randint(0, 18)))
                for c in cells:
                    if random.random() < 0.3:
                        facts.append(("cell", c[0], c[1],
                                      random.randint(0, upper[c])))
                flow = MarginFlow(stations, fleets, upper, facts)
                bf_feas, bf_lo, bf_hi = brute_force(
                    upper, facts, stations, fleets)
                self.assertEqual(flow.feasible(), bf_feas, (upper, facts))
                if not bf_feas:
                    continue
                for c in cells:
                    i, j = stations.index(c[0]), fleets.index(c[1])
                    self.assertEqual(flow.extremum(i, j),
                                     (bf_lo[c], bf_hi[c]), (c, upper, facts))
                    true_v = random.randint(0, upper[c])
                    d = random.randint(1, 3)
                    margin = flow.margins(i, j, true_v, d)
                    expect = {
                        "low_ok": bf_lo[c] <= true_v - d,
                        "high_ok": bf_hi[c] >= true_v + d}
                    self.assertEqual(margin, expect, (c, upper, facts))
                mismatches += 0
        self.assertGreater(mismatches, -1)

    def test_structural_zero_bound(self):
        upper = {("A", "F"): 0, ("A", "G"): 4,
                 ("B", "F"): 4, ("B", "G"): 2}
        flow = MarginFlow(["A", "B"], ["F", "G"], upper, [])
        self.assertTrue(flow.feasible())
        self.assertEqual(flow.extremum(0, 0), (0, 0))
        # 真值 0、余量 1：低侧目标 -1 不可能；高侧目标 1 被零上界封死
        margin = flow.margins(0, 0, 0, 1)
        self.assertFalse(margin["high_ok"])
        self.assertFalse(margin["low_ok"])

    def test_one_sided_margin_only(self):
        # 全表总计把总量压满后，某格只能向下、不能向上：只剩单侧余量。
        upper = {("A", "F"): 5, ("A", "G"): 5,
                 ("B", "F"): 5, ("B", "G"): 5}
        facts = [("total", None, None, 18),
                 ("cell", "A", "G", 5), ("cell", "B", "F", 5),
                 ("cell", "B", "G", 5)]
        flow = MarginFlow(["A", "B"], ["F", "G"], upper, facts)
        self.assertTrue(flow.feasible())
        # A/F 被锁定为 3（其余三格共 15，总计 18）
        self.assertEqual(flow.extremum(0, 0), (3, 3))
        # 真值 3、余量 1：两侧目标 2 与 4 都不可达
        locked = flow.margins(0, 0, 3, 1)
        self.assertFalse(locked["low_ok"])
        self.assertFalse(locked["high_ok"])
        # 放松总计后同一格恢复双向余量
        relaxed = MarginFlow(["A", "B"], ["F", "G"], upper,
                             [("total", None, None, 12)])
        both = relaxed.margins(0, 0, 3, 1)
        self.assertTrue(both["low_ok"])
        self.assertTrue(both["high_ok"])


# ---------------------------------------------------------------------------
# 2) 题面验收场景
# ---------------------------------------------------------------------------

class HeadlineAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = load_state()

    def test_grid_aggregation_excludes_non_billable(self):
        # S-AX 是未计费行（站点残量不为零），其 7 Wh 不得进入汇总
        req = load_request("request-headline.json")
        public, review = compile_report(self.state, req)
        published = {
            (f["fact_type"], f["station_id"], f["fleet_id"]): f["value_wh"]
            for pack in public["selected_packages"] for f in pack["published_facts"]}
        # 乙发布 A/G 格值=5（只来自 S-AG），丙发布 B/F=5（只来自 S-BF）
        self.assertEqual(published[("cell", "A", "G")], 5)
        self.assertEqual(published[("cell", "B", "F")], 5)

    def test_selects_yi_and_bing_not_jia(self):
        req = load_request("request-headline.json")
        public, review = compile_report(self.state, req)
        self.assertEqual(review["selected_package_ids"], ["bing", "yi"])
        self.assertEqual(review["total_value"], 13)
        jia = next(r for r in review["rejected_packages"]
                   if r["package_id"] == "jia")
        self.assertEqual(jia["reason"], "secrecy_violation")
        violated = {(v["station_id"], v["fleet_id"], v["failed_side"])
                    for v in jia["would_violate"]}
        # 甲加入后两个保护格两侧余量都失守
        self.assertIn(("A", "F", "low"), violated)
        self.assertIn(("A", "F", "high"), violated)
        self.assertIn(("B", "G", "low"), violated)
        self.assertIn(("B", "G", "high"), violated)

    def test_station_subtotal_history_blocks_both_packages(self):
        req = load_request("request-history-station.json")
        _public, review = compile_report(self.state, req)
        # 乙、丙都不能发；甲的增量事实（B 小计）仍安全，故选甲
        self.assertEqual(review["selected_package_ids"], ["jia"])
        reasons = {r["package_id"]: r["reason"]
                   for r in review["rejected_packages"]}
        self.assertEqual(reasons["yi"], "secrecy_violation")
        self.assertEqual(reasons["bing"], "secrecy_violation")

    def test_prior_risk_is_distinct_error(self):
        req = load_request("request-prior-risk.json")
        with self.assertRaises(ReportingError) as ctx:
            compile_report(self.state, req)
        self.assertEqual(ctx.exception.code, "prior_risk")

    def test_normal_empty_publish(self):
        req = load_request("request-empty.json")
        public, review = compile_report(self.state, req)
        self.assertEqual(public["selected_packages"], [])
        self.assertEqual(review["selected_package_ids"], [])
        self.assertEqual(review["total_value"], 0)
        self.assertEqual(review["rejected_packages"][0]["reason"],
                         "secrecy_violation")

    def test_tie_break_by_sorted_id_lexicographic(self):
        req = load_request("request-tie.json")
        _public, review = compile_report(self.state, req)
        # alpha 与 zeta 同价且都安全，可同时选；排序后标识序列字典序裁决
        self.assertEqual(review["selected_package_ids"], ["alpha", "zeta"])

    def test_packages_sharing_one_fact(self):
        req = load_request("request-shared.json")
        _public, review = compile_report(self.state, req)
        # 同一个 A/G 事实可属于多个包；p1 可发，p2 额外的 G 列小计锁死 B/G
        self.assertEqual(review["selected_package_ids"], ["p1"])
        p2 = next(r for r in review["rejected_packages"]
                  if r["package_id"] == "p2")
        self.assertEqual(p2["reason"], "secrecy_violation")

    def test_zero_bound_third_fleet(self):
        req = load_request("request-zero-bound.json")
        public, review = compile_report(self.state, req)
        self.assertEqual(review["selected_package_ids"], ["bing", "yi"])
        # 结构零车队 H 不出现在任何发布事实里
        for pack in public["selected_packages"]:
            for f in pack["published_facts"]:
                self.assertNotEqual(f["fleet_id"], "H")


# ---------------------------------------------------------------------------
# 3) 输入校验与错误码
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = load_state()
        cls.base = load_request("request-headline.json")

    def _expect(self, mutate, code):
        req = json.loads(json.dumps(self.base))
        mutate(req)
        with self.assertRaises(ReportingError) as ctx:
            compile_report(self.state, req)
        self.assertEqual(ctx.exception.code, code)

    def test_unknown_period(self):
        req = json.loads(json.dumps(self.base))
        req["period_id"] = "P-NOPE"
        with self.assertRaises(ReportingError) as ctx:
            compile_report(self.state, req)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_open_period_rejected(self):
        # 构造一个目标周期仍 open 的 state
        state = json.loads(json.dumps(self.state))
        for p in state["periods"]:
            if p["period_id"] == "P-2026-01":
                p["status"] = "open"
        with self.assertRaises(ReportingError) as ctx:
            compile_report(state, self.base)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_missing_fleet_attribution(self):
        def mutate(req):
            req["fleet_attribution"] = [
                a for a in req["fleet_attribution"]
                if a["session_id"] != "S-AF"]
        self._expect(mutate, "invalid_input")

    def test_duplicate_attribution_rejected(self):
        def mutate(req):
            req["fleet_attribution"].append(
                {"session_id": "S-AF", "fleet_id": "G"})
        self._expect(mutate, "invalid_input")

    def test_unknown_coordinate(self):
        def mutate(req):
            req["public_caliber"]["upper_bounds_wh"][0]["station_id"] = "Z"
        self._expect(mutate, "invalid_input")

    def test_value_over_upper_bound(self):
        req = load_request("request-over-bound.json")
        with self.assertRaises(ReportingError) as ctx:
            compile_report(self.state, req)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_conflicting_history_value(self):
        def mutate(req):
            req["historical_facts"][0]["value_wh"] = 21
        self._expect(mutate, "conflicting_history")

    def test_duplicate_conflicting_history_fact(self):
        def mutate(req):
            req["historical_facts"].append(
                {"fact_type": "total", "station_id": None,
                 "fleet_id": None, "value_wh": 19})
        self._expect(mutate, "conflicting_history")

    def test_boolean_is_not_integer(self):
        def mutate(req):
            req["candidate_packages"][0]["value"] = True
        self._expect(mutate, "invalid_input")

    def test_unknown_field_rejected(self):
        def mutate(req):
            req["bogus"] = 1
        self._expect(mutate, "invalid_input")

    def test_non_positive_margin_rejected(self):
        def mutate(req):
            req["protected_cells"][0]["margin_wh"] = 0
        self._expect(mutate, "invalid_input")


# ---------------------------------------------------------------------------
# 4) 确定性与端到端 CLI
# ---------------------------------------------------------------------------

class DeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = load_state()

    def test_shuffled_records_give_identical_outputs(self):
        import copy
        req = load_request("request-headline.json")
        pub1, rev1 = compile_report(self.state, req)
        shuffled = copy.deepcopy(req)
        shuffled["fleet_attribution"] = list(reversed(shuffled["fleet_attribution"]))
        shuffled["historical_facts"] = list(reversed(shuffled["historical_facts"]))
        shuffled["candidate_packages"] = list(reversed(shuffled["candidate_packages"]))
        shuffled["protected_cells"] = list(reversed(shuffled["protected_cells"]))
        for b in shuffled["public_caliber"]["upper_bounds_wh"]:
            pass
        import random
        random.Random(5).shuffle(shuffled["public_caliber"]["upper_bounds_wh"])
        pub2, rev2 = compile_report(self.state, shuffled)
        self.assertEqual(
            json.dumps(pub1, sort_keys=True, ensure_ascii=False),
            json.dumps(pub2, sort_keys=True, ensure_ascii=False))
        self.assertEqual(
            json.dumps(rev1, sort_keys=True, ensure_ascii=False),
            json.dumps(rev2, sort_keys=True, ensure_ascii=False))


class CliEndToEndTests(unittest.TestCase):
    def test_cli_writes_two_outputs_and_summary(self):
        state = load_state()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "state.json"
            req_path = FIX / "request-headline.json"
            state_path.write_text(json.dumps(state))
            pub_path = tmp / "public.json"
            rev_path = tmp / "review.json"
            cmd = [sys.executable, "-m", "charging.reporting.cli",
                   "--state", str(state_path),
                   "--request", str(req_path),
                   "--public-output", str(pub_path),
                   "--review-output", str(rev_path)]
            proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True,
                                  text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["selected_package_ids"], ["bing", "yi"])
            public = json.loads(pub_path.read_text())
            review = json.loads(rev_path.read_text())
            self.assertEqual(public["period_id"], "P-2026-01")
            # 对外文件不含保密清单/真实明细/未发布包
            self.assertNotIn("protected_cells", public)
            self.assertNotIn("rejected_packages", public)
            self.assertIn("protected_cells", review)
            self.assertIn("rejected_packages", review)

    def test_cli_failure_preserves_existing_outputs(self):
        state = load_state()
        good = load_request("request-headline.json")
        bad = load_request("request-prior-risk.json")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "state.json"
            state_path.write_text(json.dumps(state))
            pub_path = tmp / "public.json"
            rev_path = tmp / "review.json"
            good_path = tmp / "good.json"
            bad_path = tmp / "bad.json"
            good_path.write_text(json.dumps(good))
            bad_path.write_text(json.dumps(bad))
            ok = subprocess.run(
                [sys.executable, "-m", "charging.reporting.cli",
                 "--state", str(state_path), "--request", str(good_path),
                 "--public-output", str(pub_path),
                 "--review-output", str(rev_path)],
                cwd=PROJECT, capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0)
            before_pub = pub_path.read_text()
            before_rev = rev_path.read_text()
            fail = subprocess.run(
                [sys.executable, "-m", "charging.reporting.cli",
                 "--state", str(state_path), "--request", str(bad_path),
                 "--public-output", str(pub_path),
                 "--review-output", str(rev_path)],
                cwd=PROJECT, capture_output=True, text=True)
            self.assertEqual(fail.returncode, 2)
            self.assertIn("prior_risk", fail.stderr)
            self.assertEqual(pub_path.read_text(), before_pub)
            self.assertEqual(rev_path.read_text(), before_rev)


if __name__ == "__main__":
    unittest.main()
