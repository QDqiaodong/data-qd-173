"""园区上游需量电费账单复核的测试。

测试分五层：

1. 题面验收场景（四个刻钟的小例子：峰值范围、费用上下界、尚无法确证、
   证据确认、证据不支持、计价不符、计量冲突、峰值并列、有效更正取代原
   记录）通过真实 :func:`charging.engine.apply_batch` state 复核；
2. 随机小实例上与独立暴力枚举器逐一对齐：可行性、峰值精确范围、票面时段
   用量范围与证据裁定（不依赖被测实现本身的假设）；
3. 输入校验：缺边界读数、错误站点、非法时间、票面时段不在计入集合等
   非法输入抛 ``invalid_input``；
4. 确定性与规模：乱序/不同偏移写法等价输入报告逐字节一致，31 天 × 16 站、
   累计数百万 Wh 的输入在数秒内完成（不展开逐 Wh 计算）；
5. CLI 端到端：成功落盘、计量冲突仍出报告、非法输入保留原输出。
"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path

from charging.engine import apply_batch
from charging.demand import DemandError, audit_bill

PROJECT = Path(__file__).resolve().parents[1]
FIX = PROJECT / "fixtures" / "demand"

UTC = timezone.utc
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def load_state(*names):
    state = None
    for name in names:
        state, _ = apply_batch(state, json.loads((FIX / name).read_text()))
    return state


def load_audit(name):
    return json.loads((FIX / name).read_text())


def qiso(t):
    """第 t 个刻钟的起点（账期自 T0 起）。"""
    return (T0 + timedelta(minutes=15 * t)).isoformat()


# ---------------------------------------------------------------------------
# 独立解释校验器：只用报告自身列出的表源、差额与上限复核一份解释
# ---------------------------------------------------------------------------

def check_explanation(testcase, report, expl):
    metering = report["metering"]
    total = report["period"]["quarters"]
    start = datetime.fromisoformat(report["period"]["start"])
    caps = {st["station_id"]: st["quarter_cap_wh"] for st in metering["stations"]}
    quarters = expl["quarters"]
    testcase.assertEqual(len(quarters), total)
    for t, qtr in enumerate(quarters):
        testcase.assertEqual(qtr["start"],
                             (start + timedelta(minutes=15 * t)).isoformat())
        testcase.assertEqual(set(qtr["station_wh"]), set(caps))
        for sid, wh in qtr["station_wh"].items():
            testcase.assertIsInstance(wh, int)
            testcase.assertGreaterEqual(wh, 0)
            testcase.assertLessEqual(wh, caps[sid])
        testcase.assertEqual(qtr["upstream_wh"], sum(qtr["station_wh"].values()))

    def qindex(iso):
        return int((datetime.fromisoformat(iso) - start).total_seconds()) // 900

    for st in metering["stations"]:
        for iv in st["intervals"]:
            a, b = qindex(iv["start"]), qindex(iv["end"])
            got = sum(quarters[t]["station_wh"][st["station_id"]]
                      for t in range(a, b))
            testcase.assertEqual(got, iv["delta_wh"],
                                 (st["station_id"], iv["start"]))
    for iv in metering["upstream"]["intervals"]:
        a, b = qindex(iv["start"]), qindex(iv["end"])
        got = sum(quarters[t]["upstream_wh"] for t in range(a, b))
        testcase.assertEqual(got, iv["delta_wh"], iv["start"])


def counted_peak(report, expl):
    counted = set(report["analysis"]["counted_quarters"])
    return max(qtr["upstream_wh"] for qtr in expl["quarters"]
               if qtr["start"] in counted)


# ---------------------------------------------------------------------------
# 1) 题面验收场景
# ---------------------------------------------------------------------------

class HeadlineAcceptanceTests(unittest.TestCase):
    """连续四个刻钟的小例子，各表从 0 Wh 开始。"""

    @classmethod
    def setUpClass(cls):
        cls.state = load_state("batch-register.json")
        cls.audit = load_audit("audit-headline.json")

    def test_headline_undetermined(self):
        report = audit_bill(self.state, self.audit)
        self.assertEqual(report["status"], "feasible")
        # 独立峰值范围 1000..2000 Wh，对应费用 600..800 分
        self.assertEqual(report["analysis"]["peak_wh"], {"min": 1000, "max": 2000})
        self.assertEqual(report["analysis"]["billed_wh"],
                         {"min": 1500, "max": 2000})
        self.assertEqual(report["analysis"]["charge_cents"],
                         {"min": 600, "max": 800})
        # 票面算式正确（1500 = max(1000, 1500)，600 = round(1500*100/250)）
        self.assertTrue(report["bill"]["arithmetic_ok"])
        self.assertEqual(report["bill"]["expected_billed_wh"], 1500)
        self.assertEqual(report["bill"]["expected_charge_cents"], 600)
        # 有的解释支持、有的不支持：尚无法确证
        self.assertEqual(report["verdict"], "evidence_undetermined")
        ev = report["evidence"]
        self.assertEqual(ev["declared_quarter_wh"], {"min": 1000, "max": 1000})
        # 支持解释：申报时段 1000 Wh 且计入时段都不超过 1000
        check_explanation(self, report, ev["support_explanation"])
        self.assertEqual(counted_peak(report, ev["support_explanation"]), 1000)
        q4 = ev["support_explanation"]["quarters"][3]
        self.assertEqual(q4["upstream_wh"], 1000)
        # 反例：第二段计入时段用量更高（峰值 2000）
        check_explanation(self, report, ev["counterexample_explanation"])
        self.assertEqual(counted_peak(report, ev["counterexample_explanation"]),
                         2000)
        # 范围端点的两份完整解释
        check_explanation(self, report, report["analysis"]["min_peak_explanation"])
        check_explanation(self, report, report["analysis"]["max_peak_explanation"])
        self.assertEqual(
            counted_peak(report, report["analysis"]["min_peak_explanation"]), 1000)
        self.assertEqual(
            counted_peak(report, report["analysis"]["max_peak_explanation"]), 2000)

    def test_metering_section_lists_sources_records_and_deltas(self):
        report = audit_bill(self.state, self.audit)
        metering = report["metering"]
        self.assertEqual([s["station_id"] for s in metering["stations"]],
                         ["A", "B"])
        a = metering["stations"][0]
        self.assertEqual(a["import_source_id"], "M-ST-A")
        self.assertEqual(a["quarter_cap_wh"], 1000)
        self.assertEqual(
            [(iv["start_record_id"], iv["end_record_id"], iv["delta_wh"])
             for iv in a["intervals"]],
            [("rec-a-0000", "rec-a-0030", 1000),
             ("rec-a-0030", "rec-a-0060", 1000)])
        b = metering["stations"][1]
        self.assertEqual(
            [(iv["start_record_id"], iv["end_record_id"], iv["delta_wh"])
             for iv in b["intervals"]],
            [("rec-b-0000", "rec-b-0015", 500),
             ("rec-b-0015", "rec-b-0045", 1000),
             ("rec-b-0045", "rec-b-0060", 500)])
        up = metering["upstream"]
        self.assertEqual(up["source_id"], "M-CAMPUS-FEED")
        self.assertEqual(
            [(iv["start_record_id"], iv["end_record_id"], iv["delta_wh"])
             for iv in up["intervals"]],
            [("up-0000", "up-0045", 3000), ("up-0045", "up-0060", 1000)])
        self.assertEqual(metering["station_total_wh"], 4000)
        self.assertEqual(metering["upstream_total_wh"], 4000)

    def test_extra_reading_confirms_bill(self):
        # 同一账单只增加 A 站第一段末 1000 Wh 的有效读数：峰值固定 1000
        state = load_state("batch-register-confirm.json")
        report = audit_bill(state, self.audit)
        self.assertEqual(report["analysis"]["peak_wh"],
                         {"min": 1000, "max": 1000})
        self.assertEqual(report["verdict"], "evidence_confirmed")
        # 范围端点相同也有可复核的解释
        check_explanation(self, report, report["analysis"]["min_peak_explanation"])
        check_explanation(self, report, report["analysis"]["max_peak_explanation"])
        self.assertNotIn("counterexample_explanation",
                         report.get("evidence", {}))

    def test_declared_peak_not_forced_by_any_reading_is_unsupported(self):
        # 改报第四段 1250 Wh：落在原峰值范围内且金额仍为 600，仍证据不支持
        report = audit_bill(self.state, load_audit("audit-unsupported.json"))
        self.assertTrue(report["bill"]["arithmetic_ok"])
        self.assertEqual(report["bill"]["expected_charge_cents"], 600)
        self.assertEqual(report["verdict"], "evidence_unsupported")
        self.assertEqual(report["analysis"]["peak_wh"],
                         {"min": 1000, "max": 2000})

    def test_billing_mismatch_reports_expected_values_and_range(self):
        report = audit_bill(self.state, load_audit("audit-billing-mismatch.json"))
        self.assertEqual(report["verdict"], "billing_mismatch")
        self.assertFalse(report["bill"]["arithmetic_ok"])
        # 应该填的值
        self.assertEqual(report["bill"]["expected_billed_wh"], 1500)
        self.assertEqual(report["bill"]["expected_charge_cents"], 600)
        # 仍给出独立的计量范围
        self.assertEqual(report["analysis"]["peak_wh"],
                         {"min": 1000, "max": 2000})
        self.assertEqual(report["analysis"]["charge_cents"],
                         {"min": 600, "max": 800})

    def test_metering_conflict_is_report_not_error(self):
        # 总电量相符（4000 = 4000）但上游第一段区间无解（100 < 500）
        report = audit_bill(self.state, load_audit("audit-conflict.json"))
        self.assertEqual(report["status"], "metering_conflict")
        self.assertNotIn("analysis", report)
        self.assertNotIn("verdict", report)
        self.assertNotIn("bill", report)
        self.assertEqual(report["metering"]["station_total_wh"], 4000)
        self.assertEqual(report["metering"]["upstream_total_wh"], 4000)

    def test_peak_tie_accepts_any_tied_quarter(self):
        # u1 = u2 = 1000 被读数锁定：申报任一并列时段都应证据确认
        state = load_state("batch-register-tie.json")
        audit = load_audit("audit-tie.json")
        report = audit_bill(state, audit)
        self.assertEqual(report["analysis"]["peak_wh"],
                         {"min": 1000, "max": 1000})
        self.assertEqual(report["verdict"], "evidence_confirmed")
        other = json.loads(json.dumps(audit))
        other["bill"]["declared_peak_quarter_start"] = "2026-03-01T00:00:00Z"
        report2 = audit_bill(state, other)
        self.assertEqual(report2["verdict"], "evidence_confirmed")

    def test_correction_supersedes_original_record(self):
        # 有效更正取代原记录：900 -> 1000，报告用头记录标识
        state = load_state("batch-register-precorrection.json",
                           "batch-correct.json")
        report = audit_bill(state, self.audit)
        self.assertEqual(report["analysis"]["peak_wh"],
                         {"min": 1000, "max": 2000})
        self.assertEqual(report["verdict"], "evidence_undetermined")
        a = report["metering"]["stations"][0]
        self.assertEqual(a["intervals"][0]["end_record_id"], "rec-a-0030-c2")
        self.assertEqual(a["intervals"][0]["delta_wh"], 1000)
        # 被替代的原记录 rec-a-0030 不出现在报告的任何记录标识中
        record_ids = [iv[key] for st in report["metering"]["stations"]
                      for iv in st["intervals"]
                      for key in ("start_record_id", "end_record_id")]
        record_ids += [iv[key] for iv in
                       report["metering"]["upstream"]["intervals"]
                       for key in ("start_record_id", "end_record_id")]
        self.assertNotIn("rec-a-0030", record_ids)

    def test_state_is_not_mutated(self):
        before = json.dumps(self.state, sort_keys=True)
        audit_bill(self.state, self.audit)
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)

    def test_charge_rounds_half_up_once_on_total(self):
        # 15 Wh × 25 分/kW / 250 = 1.5 分，四舍五入到 2 分（精确值一次舍入）
        audit = json.loads(json.dumps(self.audit))
        audit["bill"]["minimum_demand_wh"] = 15
        audit["bill"]["rate_cents_per_kw"] = 25
        audit["bill"]["declared_peak_wh"] = 0
        audit["bill"]["declared_billed_wh"] = 15
        audit["bill"]["declared_charge_cents"] = 1
        report = audit_bill(self.state, audit)
        self.assertEqual(report["bill"]["expected_billed_wh"], 15)
        self.assertEqual(report["bill"]["expected_charge_cents"], 2)
        self.assertEqual(report["verdict"], "billing_mismatch")
        # 票面填 2 则算式正确（但 u[q4] 恒为 1000 ≠ 0，证据不支持）
        audit["bill"]["declared_charge_cents"] = 2
        report = audit_bill(self.state, audit)
        self.assertTrue(report["bill"]["arithmetic_ok"])
        self.assertEqual(report["verdict"], "evidence_unsupported")

    def test_single_quarter_period(self):
        state = {"version": 1,
                 "stations": {"A": {"import_source_id": "M-ST-A"}},
                 "chains": [
                     {"source_id": "M-ST-A", "meter_instant": qiso(0),
                      "head_record_id": "a0", "effective_cumulative_wh": 0,
                      "chain": []},
                     {"source_id": "M-ST-A", "meter_instant": qiso(1),
                      "head_record_id": "a1", "effective_cumulative_wh": 700,
                      "chain": []}]}
        audit = {
            "period_id": "P-one",
            "period": {"start": qiso(0), "end": qiso(1)},
            "stations": [{"station_id": "A", "quarter_cap_wh": 1000}],
            "upstream": {"source_id": "M-UP", "readings": [
                {"record_id": "u0", "meter_instant": qiso(0),
                 "cumulative_wh": 0},
                {"record_id": "u1", "meter_instant": qiso(1),
                 "cumulative_wh": 700}]},
            "bill": {"counted_quarters": [qiso(0)],
                     "minimum_demand_wh": 0,
                     "rate_cents_per_kw": 100,
                     "declared_peak_quarter_start": qiso(0),
                     "declared_peak_wh": 700,
                     "declared_billed_wh": 700,
                     "declared_charge_cents": 280},
        }
        report = audit_bill(state, audit)
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["analysis"]["peak_wh"], {"min": 700, "max": 700})
        self.assertEqual(report["verdict"], "evidence_confirmed")


# ---------------------------------------------------------------------------
# 2) 暴力枚举交叉校验
# ---------------------------------------------------------------------------

def _compositions(total, k, cap):
    if k == 0:
        if total == 0:
            yield ()
        return
    for v in range(min(cap, total) + 1):
        for rest in _compositions(total - v, k - 1, cap):
            yield (v,) + rest


def brute_force_feasible(stations, up_intervals, T):
    """枚举全部逐站逐刻钟整数分摊，返回满足全部区间差额的上游用量序列集合。"""
    per_station = []
    for st in stations:
        allocs = [()]
        for a, b, delta in st["intervals"]:
            comps = list(_compositions(delta, b - a, st["cap"]))
            allocs = [head + c for head in allocs for c in comps]
        per_station.append(allocs)
    feasible = []
    for combo in product(*per_station):
        u = tuple(sum(combo[s][t] for s in range(len(stations)))
                  for t in range(T))
        if all(sum(u[a:b]) == delta for a, b, delta in up_intervals):
            feasible.append(u)
    return feasible


def brute_verdict(feasible, counted, q, claimed):
    if not feasible:
        return {"verdict": "conflict"}
    peaks = [max(u[t] for t in counted) for u in feasible]
    support = any(u[q] == claimed and max(u[t] for t in counted) == claimed
                  for u in feasible)
    counter = any(not (u[q] == claimed and max(u[t] for t in counted) == claimed)
                  for u in feasible)
    if not support:
        verdict = "evidence_unsupported"
    elif counter:
        verdict = "evidence_undetermined"
    else:
        verdict = "evidence_confirmed"
    return {"peak_min": min(peaks), "peak_max": max(peaks),
            "q_min": min(u[q] for u in feasible),
            "q_max": max(u[q] for u in feasible),
            "verdict": verdict}


class BruteForceCrossCheckTests(unittest.TestCase):
    """随机小实例：流量实现与“枚举所有整数分摊”完全对齐。"""

    def _random_instance(self, rng):
        T = rng.randint(2, 4)
        n_st = rng.randint(1, 3)
        stations = []
        state_chains = []
        audit_stations = []
        for s in range(n_st):
            sid = f"ST{s}"
            src = f"M-{sid}"
            cap = rng.randint(1, 3)
            cuts = sorted(rng.sample(range(1, T), rng.randint(0, T - 1)))
            bounds = [0] + cuts + [T]
            intervals = []
            cum = 0
            recs = [(0, 0)]
            for idx, (a, b) in enumerate(zip(bounds, bounds[1:])):
                if idx > 0:
                    recs.append((a, cum))
                delta = rng.randint(0, cap * (b - a))
                intervals.append((a, b, delta))
                cum += delta
            recs.append((T, cum))
            for q, value in recs:
                state_chains.append({
                    "source_id": src,
                    "meter_instant": qiso(q),
                    "head_record_id": f"rec-{sid}-{q}",
                    "effective_cumulative_wh": value,
                    "chain": []})
            stations.append({"station_id": sid, "cap": cap,
                             "intervals": intervals})
            audit_stations.append({"station_id": sid, "quarter_cap_wh": cap})

        # 上游区间：随机划分；差额取自一份真实分摊（保证多数实例可行），
        # 小概率扰动出不可行实例
        true_u = [0] * T
        for st in stations:
            for a, b, delta in st["intervals"]:
                comp = rng.choice(list(_compositions(delta, b - a, st["cap"])))
                for i, v in enumerate(comp):
                    true_u[a + i] += v
        cuts = sorted(rng.sample(range(1, T), rng.randint(0, T - 1)))
        bounds = [0] + cuts + [T]
        up_intervals = []
        up_readings = []
        cum = 0
        for idx, (a, b) in enumerate(zip(bounds, bounds[1:])):
            if idx > 0:
                up_readings.append({"record_id": f"up-{a}",
                                    "meter_instant": qiso(a),
                                    "cumulative_wh": cum})
            delta = sum(true_u[a:b])
            if rng.random() < 0.15:
                delta += rng.choice((-1, 1))
                delta = max(0, delta)
            up_intervals.append((a, b, delta))
            cum += delta
        up_readings.append({"record_id": f"up-{T}", "meter_instant": qiso(T),
                            "cumulative_wh": cum})

        counted = sorted(rng.sample(range(T), rng.randint(1, T)))
        q = rng.choice(counted)
        claimed = rng.randint(0, 2 * n_st * 3)
        minimum = rng.choice((0, 1, 3))
        rate = rng.choice((0, 7, 100))
        billed = max(claimed, minimum)
        charge = (2 * billed * rate + 250) // 500  # round_half_up(billed*rate/250)

        state = {"version": 1,
                 "stations": {st["station_id"]:
                              {"import_source_id": f"M-{st['station_id']}"}
                              for st in stations},
                 "chains": state_chains}
        audit = {
            "period_id": "P-random",
            "period": {"start": qiso(0), "end": qiso(T)},
            "stations": audit_stations,
            "upstream": {"source_id": "M-UP", "readings": [
                {"record_id": "up-0", "meter_instant": qiso(0),
                 "cumulative_wh": 0}] + up_readings},
            "bill": {
                "counted_quarters": [qiso(t) for t in counted],
                "minimum_demand_wh": minimum,
                "rate_cents_per_kw": rate,
                "declared_peak_quarter_start": qiso(q),
                "declared_peak_wh": claimed,
                "declared_billed_wh": billed,
                "declared_charge_cents": charge,
            },
        }
        meta = {"stations": stations, "up_intervals": up_intervals, "T": T,
                "counted": counted, "q": q, "claimed": claimed}
        return state, audit, meta

    def test_matches_brute_force(self):
        import random
        rng = random.Random(20260301)
        checked = {"feasible": 0, "conflict": 0}
        for _ in range(300):
            state, audit, meta = self._random_instance(rng)
            report = audit_bill(state, audit)
            feasible = brute_force_feasible(meta["stations"],
                                            meta["up_intervals"], meta["T"])
            expect = brute_verdict(feasible, meta["counted"], meta["q"],
                                   meta["claimed"])
            if expect["verdict"] == "conflict":
                self.assertEqual(report["status"], "metering_conflict", meta)
                checked["conflict"] += 1
                continue
            self.assertEqual(report["status"], "feasible", meta)
            checked["feasible"] += 1
            self.assertEqual(report["analysis"]["peak_wh"]["min"],
                             expect["peak_min"], meta)
            self.assertEqual(report["analysis"]["peak_wh"]["max"],
                             expect["peak_max"], meta)
            self.assertEqual(report["evidence"]["declared_quarter_wh"],
                             {"min": expect["q_min"], "max": expect["q_max"]},
                             meta)
            self.assertEqual(report["verdict"], expect["verdict"], meta)
            # 附带的解释必须独立可复核
            analysis = report["analysis"]
            check_explanation(self, report, analysis["min_peak_explanation"])
            check_explanation(self, report, analysis["max_peak_explanation"])
            self.assertEqual(
                counted_peak(report, analysis["min_peak_explanation"]),
                expect["peak_min"], meta)
            self.assertEqual(
                counted_peak(report, analysis["max_peak_explanation"]),
                expect["peak_max"], meta)
            ev = report["evidence"]
            if report["verdict"] == "evidence_undetermined":
                check_explanation(self, report, ev["support_explanation"])
                check_explanation(self, report,
                                  ev["counterexample_explanation"])
                sup = ev["support_explanation"]["quarters"]
                self.assertEqual(sup[meta["q"]]["upstream_wh"], meta["claimed"])
                self.assertEqual(
                    counted_peak(report, ev["support_explanation"]),
                    meta["claimed"])
                ctr = ev["counterexample_explanation"]["quarters"]
                ctr_ok = (ctr[meta["q"]]["upstream_wh"] == meta["claimed"]
                          and counted_peak(report,
                                           ev["counterexample_explanation"])
                          == meta["claimed"])
                self.assertFalse(ctr_ok, meta)
        self.assertGreater(checked["feasible"], 200)
        self.assertGreater(checked["conflict"], 5)


# ---------------------------------------------------------------------------
# 3) 输入校验
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = load_state("batch-register.json")
        cls.base = load_audit("audit-headline.json")

    def _expect(self, mutate, needle=None, state=None):
        audit = json.loads(json.dumps(self.base))
        mutate(audit)
        with self.assertRaises(DemandError) as ctx:
            audit_bill(self.state if state is None else state, audit)
        self.assertEqual(ctx.exception.code, "invalid_input")
        if needle is not None:
            self.assertIn(needle, ctx.exception.message)

    def test_missing_upstream_boundary_reading(self):
        def mutate(audit):
            audit["upstream"]["readings"] = [
                r for r in audit["upstream"]["readings"]
                if r["record_id"] != "up-0060"]
        self._expect(mutate, "账期结束")

    def test_out_of_period_reading_does_not_fill_boundary(self):
        # 账期外的点不用于填补：开始时刻无读数即非法
        state = {"version": 1,
                 "stations": {"A": {"import_source_id": "M-ST-A"},
                              "B": {"import_source_id": "M-ST-B"}},
                 "chains": [
                     {"source_id": "M-ST-A", "meter_instant": "2026-02-28T23:45:00Z",
                      "head_record_id": "x1", "effective_cumulative_wh": 0,
                      "chain": []},
                     {"source_id": "M-ST-A", "meter_instant": qiso(4),
                      "head_record_id": "x2", "effective_cumulative_wh": 2000,
                      "chain": []},
                     {"source_id": "M-ST-B", "meter_instant": qiso(0),
                      "head_record_id": "y1", "effective_cumulative_wh": 0,
                      "chain": []},
                     {"source_id": "M-ST-B", "meter_instant": qiso(4),
                      "head_record_id": "y2", "effective_cumulative_wh": 2000,
                      "chain": []}]}
        self._expect(lambda audit: None, "账期开始", state=state)

    def test_unknown_station(self):
        def mutate(audit):
            audit["stations"].append({"station_id": "ZZ", "quarter_cap_wh": 1})
        self._expect(mutate, "错误站点")

    def test_duplicate_station(self):
        def mutate(audit):
            audit["stations"].append({"station_id": "A", "quarter_cap_wh": 1})
        self._expect(mutate, "重复")

    def test_same_import_meter_as_two_stations(self):
        batch = {"batch_id": "shared-meter",
                 "tariffs": [{"start": "2026-03-01T00:00:00+00:00",
                              "end": "2026-03-02T00:00:00+00:00",
                              "cents_per_kwh": 10}],
                 "stations": [{"station_id": "A", "import_source_id": "M-SHARED"},
                              {"station_id": "B", "import_source_id": "M-SHARED"}],
                 "records": [
                     {"record_id": "r0", "source_id": "M-SHARED",
                      "meter_instant": qiso(0), "cumulative_wh": 0},
                     {"record_id": "r1", "source_id": "M-SHARED",
                      "meter_instant": qiso(4), "cumulative_wh": 4000}]}
        state, _ = apply_batch(None, batch)
        self._expect(lambda audit: None, "同一进口表", state=state)

    def test_upstream_source_collides_with_station_meter(self):
        def mutate(audit):
            audit["upstream"]["source_id"] = "M-ST-A"
        self._expect(mutate, "上游表")

    def test_non_quarter_aligned_reading(self):
        def mutate(audit):
            audit["upstream"]["readings"][1]["meter_instant"] = \
                "2026-03-01T00:47:00Z"
        self._expect(mutate, "刻钟边界")

    def test_period_not_quarter_aligned(self):
        def mutate(audit):
            audit["period"]["start"] = "2026-03-01T00:05:00Z"
        self._expect(mutate, "刻钟边界")

    def test_period_empty(self):
        def mutate(audit):
            audit["period"]["end"] = audit["period"]["start"]
        self._expect(mutate)

    def test_negative_cumulative(self):
        def mutate(audit):
            audit["upstream"]["readings"][0]["cumulative_wh"] = -1
        self._expect(mutate, "非负整数")

    def test_decreasing_cumulative(self):
        def mutate(audit):
            audit["upstream"]["readings"][1]["cumulative_wh"] = 5000
        self._expect(mutate, "倒退")

    def test_duplicate_upstream_record_id(self):
        def mutate(audit):
            audit["upstream"]["readings"][1]["record_id"] = "up-0000"
        self._expect(mutate, "重复")

    def test_peak_quarter_not_in_counted_set(self):
        def mutate(audit):
            audit["bill"]["declared_peak_quarter_start"] = "2026-03-01T00:00:00Z"
        self._expect(mutate, "不在计入需量的刻钟集合中")

    def test_empty_counted_set(self):
        def mutate(audit):
            audit["bill"]["counted_quarters"] = []
        self._expect(mutate, "非空数组")

    def test_counted_quarter_outside_period(self):
        def mutate(audit):
            audit["bill"]["counted_quarters"].append("2026-03-01T01:00:00Z")
        self._expect(mutate, "不在账期")

    def test_duplicate_counted_quarter(self):
        def mutate(audit):
            audit["bill"]["counted_quarters"].append(
                audit["bill"]["counted_quarters"][0])
        self._expect(mutate, "重复")

    def test_boolean_is_not_integer(self):
        def mutate(audit):
            audit["stations"][0]["quarter_cap_wh"] = True
        self._expect(mutate, "非负整数")

    def test_negative_cap(self):
        def mutate(audit):
            audit["stations"][0]["quarter_cap_wh"] = -5
        self._expect(mutate, "非负整数")

    def test_bad_time_format(self):
        def mutate(audit):
            audit["period"]["start"] = "2026年3月1日"
        self._expect(mutate, "无法解析")

    def test_naive_time_rejected(self):
        def mutate(audit):
            audit["period"]["start"] = "2026-03-01T00:00:00"
        self._expect(mutate, "显式偏移")

    def test_unknown_field_rejected(self):
        def mutate(audit):
            audit["bogus"] = 1
        self._expect(mutate, "未知字段")

    def test_unknown_bill_field_rejected(self):
        def mutate(audit):
            audit["bill"]["discount"] = 3
        self._expect(mutate, "未知字段")

    def test_bad_state_version(self):
        state = json.loads(json.dumps(self.state))
        state["version"] = 2
        self._expect(lambda audit: None, "version", state=state)


# ---------------------------------------------------------------------------
# 4) 确定性与等价输入
# ---------------------------------------------------------------------------

class DeterminismTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = load_state("batch-register.json")
        cls.base = load_audit("audit-headline.json")

    def test_shuffled_equivalent_inputs_give_identical_reports(self):
        import random
        rng = random.Random(7)
        audit = json.loads(json.dumps(self.base))
        rng.shuffle(audit["stations"])
        rng.shuffle(audit["upstream"]["readings"])
        rng.shuffle(audit["bill"]["counted_quarters"])
        r1 = audit_bill(self.state, self.base)
        r2 = audit_bill(self.state, audit)
        self.assertEqual(json.dumps(r1, sort_keys=True, ensure_ascii=False),
                         json.dumps(r2, sort_keys=True, ensure_ascii=False))

    def test_offset_rewrite_gives_identical_reports(self):
        # 同一瞬时的不同偏移写法（按实际瞬时归一为 UTC）
        audit = json.loads(json.dumps(self.base))
        audit["period"]["start"] = "2026-03-01T01:00:00+01:00"
        audit["period"]["end"] = "2026-03-01T02:00:00+01:00"
        audit["upstream"]["readings"] = [
            {"record_id": "up-0000", "meter_instant": "2026-03-01T01:00:00+01:00",
             "cumulative_wh": 0},
            {"record_id": "up-0045", "meter_instant": "2026-03-01T01:45:00+01:00",
             "cumulative_wh": 3000},
            {"record_id": "up-0060", "meter_instant": "2026-03-01T02:00:00+01:00",
             "cumulative_wh": 4000}]
        audit["bill"]["counted_quarters"] = ["2026-03-01T01:15:00+01:00",
                                             "2026-03-01T01:45:00+01:00"]
        audit["bill"]["declared_peak_quarter_start"] = "2026-03-01T01:45:00+01:00"
        r1 = audit_bill(self.state, self.base)
        r2 = audit_bill(self.state, audit)
        self.assertEqual(json.dumps(r1, sort_keys=True, ensure_ascii=False),
                         json.dumps(r2, sort_keys=True, ensure_ascii=False))

    def test_out_of_period_readings_are_ignored(self):
        audit = json.loads(json.dumps(self.base))
        audit["upstream"]["readings"].append(
            {"record_id": "up-0075", "meter_instant": "2026-03-01T01:15:00Z",
             "cumulative_wh": 4000})
        r1 = audit_bill(self.state, self.base)
        r2 = audit_bill(self.state, audit)
        self.assertEqual(json.dumps(r1, sort_keys=True, ensure_ascii=False),
                         json.dumps(r2, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 5) 规模：31 天 × 16 站，累计数百万 Wh
# ---------------------------------------------------------------------------

def build_scale_instance(days=31, n_stations=16):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    T = days * 96

    def iso(dt):
        return dt.astimezone(UTC).isoformat()

    state = {"version": 1, "stations": {}, "chains": []}
    stations_in = []
    for s in range(n_stations):
        sid = f"S{s + 1:02d}"
        state["stations"][sid] = {"import_source_id": f"M-ST-{sid}"}
        stations_in.append({"station_id": sid, "quarter_cap_wh": 2000})
    for s in range(n_stations):
        cum = 0
        for d in range(days + 1):
            state["chains"].append({
                "source_id": f"M-ST-S{s + 1:02d}",
                "meter_instant": iso(start + timedelta(days=d)),
                "head_record_id": f"rec-s{s:02d}-d{d:02d}",
                "effective_cumulative_wh": cum, "chain": []})
            if d < days:
                cum += (s * 977 + d * 613) % 9000 + 6000
    cum = 0
    up_readings = []
    for d in range(days + 1):
        up_readings.append({"record_id": f"up-d{d:02d}",
                            "meter_instant": iso(start + timedelta(days=d)),
                            "cumulative_wh": cum})
        if d < days:
            cum += sum((s * 977 + d * 613) % 9000 + 6000
                       for s in range(n_stations))
    audit = {
        "period_id": "P-2026-01",
        "period": {"start": iso(start), "end": iso(start + timedelta(days=days))},
        "stations": stations_in,
        "upstream": {"source_id": "M-FEED", "readings": up_readings},
        "bill": {
            "counted_quarters": [iso(start + timedelta(minutes=15 * i))
                                 for i in range(T)],
            "minimum_demand_wh": 3000,
            "rate_cents_per_kw": 100,
            "declared_peak_quarter_start": iso(start),
            "declared_peak_wh": 5000,
            "declared_billed_wh": 5000,
            "declared_charge_cents": 2000,
        },
    }
    return state, audit


class ScaleTests(unittest.TestCase):
    def test_31_days_16_stations_millions_of_wh(self):
        state, audit = build_scale_instance()
        report = audit_bill(state, audit)
        self.assertEqual(report["status"], "feasible")
        self.assertEqual(report["period"]["quarters"], 31 * 96)
        self.assertGreater(report["metering"]["upstream_total_wh"], 5_000_000)
        self.assertEqual(report["analysis"]["peak_wh"],
                         {"min": 1889, "max": 32000})
        self.assertEqual(report["verdict"], "evidence_undetermined")
        # 逐站逐刻钟完整解释可独立复核（2976 刻钟 × 16 站）
        analysis = report["analysis"]
        check_explanation(self, report, analysis["min_peak_explanation"])
        check_explanation(self, report, analysis["max_peak_explanation"])
        self.assertEqual(counted_peak(report, analysis["min_peak_explanation"]),
                         1889)
        self.assertEqual(counted_peak(report, analysis["max_peak_explanation"]),
                         32000)
        ev = report["evidence"]
        check_explanation(self, report, ev["support_explanation"])
        check_explanation(self, report, ev["counterexample_explanation"])
        json.dumps(report)  # 报告必须可 JSON 序列化


# ---------------------------------------------------------------------------
# 6) CLI 端到端
# ---------------------------------------------------------------------------

class CliEndToEndTests(unittest.TestCase):
    def _run_cli(self, state_path, audit_path, output_path):
        cmd = [sys.executable, "-m", "charging.demand.cli",
               "--state", str(state_path), "--audit", str(audit_path)]
        if output_path is not None:
            cmd += ["--output", str(output_path)]
        return subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)

    def test_cli_success_writes_report_and_summary(self):
        state = load_state("batch-register.json")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "state.json"
            state_path.write_text(json.dumps(state))
            out_path = tmp / "report.json"
            proc = self._run_cli(state_path, FIX / "audit-headline.json",
                                 out_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["verdict"], "evidence_undetermined")
            self.assertEqual(summary["peak_wh"], {"min": 1000, "max": 2000})
            report = json.loads(out_path.read_text())
            self.assertEqual(report["period_id"], "P-2026-03-01-peak")
            self.assertEqual(report["verdict"], "evidence_undetermined")

    def test_cli_metering_conflict_exits_zero_with_report(self):
        state = load_state("batch-register.json")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "state.json"
            state_path.write_text(json.dumps(state))
            out_path = tmp / "report.json"
            proc = self._run_cli(state_path, FIX / "audit-conflict.json",
                                 out_path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["status"], "metering_conflict")
            self.assertNotIn("verdict", summary)
            report = json.loads(out_path.read_text())
            self.assertEqual(report["status"], "metering_conflict")

    def test_cli_invalid_input_preserves_existing_output(self):
        state = load_state("batch-register.json")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "state.json"
            state_path.write_text(json.dumps(state))
            out_path = tmp / "report.json"
            ok = self._run_cli(state_path, FIX / "audit-headline.json", out_path)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            before = out_path.read_text()
            bad_audit = tmp / "bad.json"
            bad = load_audit("audit-headline.json")
            bad["upstream"]["readings"] = bad["upstream"]["readings"][:-1]
            bad_audit.write_text(json.dumps(bad))
            fail = self._run_cli(state_path, bad_audit, out_path)
            self.assertEqual(fail.returncode, 2)
            self.assertIn("invalid_input", fail.stderr)
            self.assertEqual(out_path.read_text(), before)

    def test_cli_without_output_prints_summary_only(self):
        state = load_state("batch-register.json")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(json.dumps(state))
            proc = self._run_cli(state_path, FIX / "audit-tie.json", None)
            # 账期/站点与 tie 夹具不符时应失败；改用 headline
            proc = self._run_cli(state_path, FIX / "audit-headline.json", None)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertEqual(summary["status"], "feasible")


if __name__ == "__main__":
    unittest.main()
