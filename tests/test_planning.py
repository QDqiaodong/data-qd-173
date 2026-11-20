"""车队补能 / 降载邀约联合排程测试。

测试不依赖求解器内部状态，而是把 ``plan`` 发布的结果当作外部产物，逐情景
独立复核：

* 每辆车计划总量恰好等于需求、只在在站窗口内充电、不超每段车端上限；
* 逐段站点使用量不超过该情景的有效额度（常态为正常额度，应急为扣减后
  额度），所有能量为非负整数；
* 应急情景在激活开始前与常态计划**逐车逐段完全相同**（共同前缀）；
* 邀约决定（接受集合、报酬、拒绝原因）与组合裁决规则。

覆盖：题面四项验收（冲突、同报酬按标识裁决、降载后补回、多项合计压过
单项）、无邀约可行/不可行、非法输入与常态不可行的可区分错误码、乱序
输入稳定性，以及 CLI 失败保留原输出文件。
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from charging.planning import PLAN_VERSION, PlanningError, plan

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIX = PROJECT_ROOT / "fixtures" / "planning"


def load(name):
    return json.loads((FIX / name).read_text())


def scenario(result, scenario_id):
    return next(s for s in result["scenarios"] if s["scenario_id"] == scenario_id)


def audit_scenario(result, scen, fleet, offer_by_id):
    """对发布的单个情景做独立守恒/容量/窗口复核，返回逐车计划矩阵。"""
    n = result["slot_count"]
    assert result["normal_capacity_wh"] == fleet["normal_capacity_wh"]
    matrix = {}
    # 车辆需求与在站窗口
    fv = {v["vehicle_id"]: v for v in fleet["vehicles"]}
    assert {x["vehicle_id"] for x in scen["vehicles"]} == set(fv)
    for row in scen["vehicles"]:
        vid = row["vehicle_id"]
        v = fv[vid]
        alloc = row["charge_wh_by_slot"]
        assert isinstance(alloc, list) and len(alloc) == n
        # 总量恰好等于需求，不靠超充
        assert row["planned_energy_wh"] == sum(alloc) == v["required_energy_wh"]
        for t, x in enumerate(alloc):
            assert isinstance(x, int) and x >= 0
            # 只在在站窗口内（含到站、不含离站）
            if not (v["arrival_slot"] <= t < v["departure_slot"]):
                assert x == 0
            # 车端每段上限
            assert x <= v["max_charge_wh"][t]
        matrix[vid] = alloc

    # 逐段站点使用量与有效额度
    usage = [0] * n
    for vid, alloc in matrix.items():
        for t, x in enumerate(alloc):
            usage[t] += x
    for t, slot in enumerate(scen["slots"]):
        assert slot["slot"] == t
        assert slot["station_used_wh"] == usage[t]
        eff = slot["normal_capacity_wh"] - slot["reduction_wh"]
        assert slot["effective_capacity_wh"] == eff
        assert usage[t] <= eff                      # 站点额度（扣减后）
        assert slot["spare_capacity_wh"] == eff - usage[t]

    # 应急情景：激活前与常态逐车逐段相同
    if scen["kind"] == "contingency":
        offer = offer_by_id[scen["offer_id"]]
        start = offer["start_slot"]
        assert scen["activation_start_slot"] == start
        assert scen["prefix_locked_through_slot"] == start
        base = scenario(result, "baseline")
        base_m = {v["vehicle_id"]: v["charge_wh_by_slot"] for v in base["vehicles"]}
        for vid, alloc in matrix.items():
            for t in range(start):
                assert alloc[t] == base_m[vid][t], \
                    f"{scen['offer_id']} 激活前时段 {t} 车辆 {vid} 与常态不一致"
        # 扣减段与声明一致
        for t in range(n):
            in_window = offer["start_slot"] <= t < offer["end_slot"]
            assert scen["slots"][t]["reduction_wh"] == \
                (offer["reduction_wh"] if in_window else 0)
    else:
        assert scen["prefix_locked_through_slot"] is None
        assert all(slot["reduction_wh"] == 0 for slot in scen["slots"])
    return matrix


def audit_result(result, fleet, offers_doc):
    """对整份发布结果做端到端复核。``offers_doc`` 可为完整文件或邀约数组。"""
    offers = offers_doc["offers"] if isinstance(offers_doc, dict) else offers_doc
    offer_by_id = {o["offer_id"]: o for o in offers}
    assert result["plan_version"] == PLAN_VERSION
    assert result["station_id"] == fleet["station_id"]
    assert result["slot_count"] == len(fleet["normal_capacity_wh"])
    matrices = {}
    for scen in result["scenarios"]:
        matrices[scen["scenario_id"]] = audit_scenario(
            result, scen, fleet, offer_by_id)
    # 接受列表与情景集合一致
    accepted = result["decision"]["accepted_offer_ids"]
    scen_ids = {s["offer_id"] for s in result["scenarios"] if s["kind"] == "contingency"}
    assert set(accepted) == scen_ids
    assert result["decision"]["accepted_count"] == len(accepted)
    assert result["decision"]["total_reward"] == \
        sum(offer_by_id[i]["reward"] for i in accepted)
    # 每个邀约都有决定，且字段自洽
    assert {d["offer_id"] for d in result["offer_decisions"]} == set(offer_by_id)
    for d in result["offer_decisions"]:
        assert d["accepted"] == (d["offer_id"] in accepted)
        if d["accepted"]:
            assert d["reject_reason"] is None
            assert d["reward_if_accepted"] == offer_by_id[d["offer_id"]]["reward"]
        else:
            assert d["reward_if_accepted"] == 0
            assert d["reject_reason"] in ("infeasible_alone", "not_in_best_combination")
    return matrices


class HeadlineConflictTests(unittest.TestCase):
    """题面关键验收：四个时段、两车、两项互斥冲突邀约。"""

    def setUp(self):
        self.fleet = load("fleet-headline.json")
        self.offers = load("offers-headline.json")
        self.result = plan(self.fleet, self.offers)

    def test_published_schedules_are_independently_feasible(self):
        audit_result(self.result, self.fleet, self.offers)

    def test_only_higher_reward_o2_accepted(self):
        self.assertEqual(self.result["decision"]["accepted_offer_ids"], ["O2"])
        self.assertEqual(self.result["decision"]["total_reward"], 9)
        decision = {d["offer_id"]: d for d in self.result["offer_decisions"]}
        self.assertTrue(decision["O1"]["feasible_alone"])
        self.assertFalse(decision["O1"]["accepted"])
        self.assertEqual(decision["O1"]["reject_reason"], "not_in_best_combination")

    def test_o1_alone_requires_slot0_for_A(self):
        # 单独签 O1：第二段额度被打到 0，A 只能在第 0 段补能。
        only_o1 = copy.deepcopy(self.offers)
        only_o1["offers"] = [o for o in only_o1["offers"] if o["offer_id"] == "O1"]
        r = plan(self.fleet, only_o1)
        self.assertEqual(r["decision"]["accepted_offer_ids"], ["O1"])
        m = audit_result(r, self.fleet, only_o1["offers"])
        cont = m["offer:O1"]
        self.assertEqual(cont["A"], [1, 0, 0, 0])
        # B 的 2 Wh 在 O1 情景下只能落在 t0 之外的 t2/t3（t1 额度为 0）
        self.assertEqual(cont["B"][1], 0)
        self.assertEqual(sum(cont["B"]), 2)

    def test_o2_alone_requires_slot0_for_B(self):
        # 单独签 O2：后三段各只有 3/0/0 的富余，A 可在 t1 补，B 必须占 t0。
        only_o2 = copy.deepcopy(self.offers)
        only_o2["offers"] = [o for o in only_o2["offers"] if o["offer_id"] == "O2"]
        r = plan(self.fleet, only_o2)
        m = audit_result(r, self.fleet, only_o2["offers"])
        cont = m["offer:O2"]
        self.assertEqual(cont["B"][0], 1)
        self.assertEqual(cont["A"][0], 0)
        self.assertEqual(sum(cont["A"]), 1)

    def test_cannot_accept_both_with_one_shared_baseline(self):
        # 直接证明：不存在一份常态前缀同时满足两情景（引擎必须拒绝组合）。
        # O1 要求 A@t0=1；O2 要求 B@t0=1，而 t0 站点额度只有 1。
        r = self.result
        accepted = set(r["decision"]["accepted_offer_ids"])
        self.assertNotIn("O1", accepted)
        self.assertNotEqual({"O1", "O2"} <= accepted, True)
        # 且接受 O2 的计划里 t0 确实给了 B（与 O1 单独解互斥）
        base = scenario(r, "baseline")
        base_m = {v["vehicle_id"]: v["charge_wh_by_slot"] for v in base["vehicles"]}
        self.assertEqual(base_m["B"][0], 1)
        self.assertEqual(base_m["A"][0], 0)


class TieBreakTests(unittest.TestCase):
    """同报酬时按排序后邀约标识序列的字典序裁决。"""

    def test_equal_reward_prefers_lexicographically_smaller_id(self):
        fleet = load("fleet-headline.json")
        offers = load("offers-headline-tie.json")  # O1 与 O2 报酬都为 9
        r = plan(fleet, offers)
        self.assertEqual(r["decision"]["accepted_offer_ids"], ["O1"])
        self.assertEqual(r["decision"]["total_reward"], 9)
        audit_result(r, fleet, offers)
        base = scenario(r, "baseline")
        base_m = {v["vehicle_id"]: v["charge_wh_by_slot"] for v in base["vehicles"]}
        # O1 前缀要求 t0 给 A
        self.assertEqual(base_m["A"][0], 1)


class RecoveryAfterShedTests(unittest.TestCase):
    """短时降载导致电量在降载结束后补回。"""

    def test_energy_moved_to_after_event_slot(self):
        fleet = load("fleet-recovery.json")
        offers = load("offers-recovery.json")
        r = plan(fleet, offers)
        self.assertEqual(r["decision"]["accepted_offer_ids"], ["R1"])
        m = audit_result(r, fleet, offers)
        # 常态：C 在 t0、t1 补满 2 Wh
        self.assertEqual(m["baseline"]["C"], [1, 1, 0])
        # 应急：t1 降载到 0，第 1 Wh 被推迟到降载结束后的 t2
        self.assertEqual(m["offer:R1"]["C"], [1, 0, 1])
        # 激活前（t0）与常态完全一致
        self.assertEqual(m["offer:R1"]["C"][0], m["baseline"]["C"][0])
        cont = scenario(r, "offer:R1")
        self.assertEqual(cont["slots"][1]["effective_capacity_wh"], 0)
        self.assertEqual(cont["slots"][2]["station_used_wh"], 1)


class CombinationBeatsSingleTests(unittest.TestCase):
    """最值钱的单项让位给合计报酬更高的多项组合。"""

    def test_two_small_offers_together_outearn_big_single(self):
        fleet = load("fleet-combo.json")
        offers = load("offers-combo.json")
        r = plan(fleet, offers)
        accepted = r["decision"]["accepted_offer_ids"]
        self.assertEqual(accepted, ["S1", "S2"])
        self.assertEqual(r["decision"]["total_reward"], 12)  # 7 + 5 > BIG 单项 10
        audit_result(r, fleet, offers)

        # BIG 单项本身可兑现，但与 S1 的前缀要求互斥，故不在最优组合里。
        decision = {d["offer_id"]: d for d in r["offer_decisions"]}
        self.assertTrue(decision["BIG"]["feasible_alone"])
        self.assertFalse(decision["BIG"]["accepted"])

        # BIG 单独可签且报酬 10
        only_big = copy.deepcopy(offers)
        only_big["offers"] = [o for o in offers["offers"] if o["offer_id"] == "BIG"]
        rb = plan(fleet, only_big)
        self.assertEqual(rb["decision"]["accepted_offer_ids"], ["BIG"])
        self.assertEqual(rb["decision"]["total_reward"], 10)

        # 三项不可同时兑现：BIG 要求 t0 给 A，S1 要求 t0 给 B，t0 额度仅 1。
        all_ids = {o["offer_id"] for o in offers["offers"]}
        self.assertNotEqual(set(accepted), all_ids)


class StaggeredActivationTests(unittest.TestCase):
    """激活段错开的邀约：晚激活者必须锁定两次激活之间的常态段。"""

    def test_later_offer_locks_intermediate_baseline_slot(self):
        # 两个邀约都在 t0 之后激活，因此都锁定同一份常态的 t0，但对 t0 的
        # 必要安排相反：t0 站点额度仅 1 Wh，P、Q 两车在 t0 各最多充 1 Wh。
        #   E1（t1 激活，把 t1 扣到 0）：P 在 t1 之后离站、只剩 t0 可补，
        #      故常态/应急共同的 t0 必须安排 P=1。
        #   E2（t2 激活，把 t2 扣到 0）：Q 的窗口只到 t2，t1 又被其它需求占
        #      满，故共同的 t0 必须安排 Q=1。
        # 单项各自可兑现，但同一份常态 t0（容量 1）不能同时 P=1 与 Q=1。
        fleet = {
            "station_id": "ST-STAG", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30, "normal_capacity_wh": [1, 1, 1],
            "vehicles": [
                # P：可在 t0、t1 补；E1 把 t1 扣空后只剩 t0。
                {"vehicle_id": "P", "arrival_slot": 0, "departure_slot": 2,
                 "max_charge_wh": [1, 1, 0], "required_energy_wh": 1},
                # Q：可在 t0、t2 补（t1 不在站）；E2 把 t2 扣空后只剩 t0。
                {"vehicle_id": "Q", "arrival_slot": 0, "departure_slot": 3,
                 "max_charge_wh": [1, 0, 1], "required_energy_wh": 1},
            ],
        }
        offers = {
            "station_id": "ST-STAG", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30,
            "offers": [
                {"offer_id": "E1", "start_slot": 1, "end_slot": 2,
                 "reduction_wh": 1, "reward": 8},
                {"offer_id": "E2", "start_slot": 2, "end_slot": 3,
                 "reduction_wh": 1, "reward": 5},
            ],
        }
        r = plan(fleet, offers)
        decision = {d["offer_id"]: d for d in r["offer_decisions"]}
        self.assertTrue(decision["E1"]["feasible_alone"])
        self.assertTrue(decision["E2"]["feasible_alone"])
        # 互斥，只能二选一；E1 报酬 8 > E2 报酬 5。
        self.assertEqual(r["decision"]["accepted_offer_ids"], ["E1"])
        # 各自单独可签，且锁定的常态 t0 把容量给了不同的车。
        for oid, t0_vehicle in (("E1", "P"), ("E2", "Q")):
            one = copy.deepcopy(offers)
            one["offers"] = [o for o in offers["offers"] if o["offer_id"] == oid]
            rr = plan(fleet, one)
            self.assertEqual(rr["decision"]["accepted_offer_ids"], [oid])
            base = scenario(rr, "baseline")
            row = next(v for v in base["vehicles"] if v["vehicle_id"] == t0_vehicle)
            self.assertEqual(row["charge_wh_by_slot"][0], 1)

    def test_two_staggered_offers_share_full_prefix_correctly(self):
        # 三段、单段额度 1；单车 Z 需要 2 Wh，每段上限 1。
        fleet = {
            "station_id": "ST-STAG2", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30, "normal_capacity_wh": [1, 1, 1],
            "vehicles": [{"vehicle_id": "Z", "arrival_slot": 0,
                          "departure_slot": 3, "max_charge_wh": [1, 1, 1],
                          "required_energy_wh": 2}],
        }
        offers = {
            "station_id": "ST-STAG2", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30,
            "offers": [
                {"offer_id": "P", "start_slot": 1, "end_slot": 2,
                 "reduction_wh": 1, "reward": 4},   # t1 归零，需 t0+t2
                {"offer_id": "Q", "start_slot": 2, "end_slot": 3,
                 "reduction_wh": 1, "reward": 5},   # t2 归零，需 t0+t1
            ],
        }
        # 同一份常态必须让 P（锁 t0）与 Q（锁 t0、t1）都可补全：t0=1 是
        # 两者共同的必要安排；P 用 t2 补、Q 用 t1 补，互不冲突 -> 可同签。
        r = plan(fleet, offers)
        self.assertEqual(r["decision"]["accepted_offer_ids"], ["P", "Q"])
        m = audit_result(r, fleet, offers)
        self.assertEqual(m["baseline"]["Z"], [1, 1, 0])
        self.assertEqual(m["offer:P"]["Z"], [1, 0, 1])
        self.assertEqual(m["offer:Q"]["Z"], [1, 1, 0])


class NoOfferAndInfeasibleTests(unittest.TestCase):

    def test_no_offers_still_delivers_baseline(self):
        fleet = load("fleet-headline.json")
        offers = load("offers-empty.json")
        r = plan(fleet, offers)
        self.assertEqual(r["decision"]["accepted_offer_ids"], [])
        self.assertEqual(r["decision"]["total_reward"], 0)
        self.assertEqual([s["scenario_id"] for s in r["scenarios"]], ["baseline"])
        audit_result(r, fleet, offers)

    def test_legal_input_but_baseline_infeasible_is_distinct_error(self):
        fleet = load("fleet-infeasible.json")
        offers = load("offers-infeasible.json")
        with self.assertRaises(PlanningError) as ctx:
            plan(fleet, offers)
        self.assertEqual(ctx.exception.code, "baseline_infeasible")

    def test_offer_that_makes_vehicles_miss_needs_is_business_unacceptable(self):
        # 合法邀约（扣减不超正常额度），但激活后车辆无法按时补能。
        fleet = {
            "station_id": "ST-X", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30, "normal_capacity_wh": [1, 1],
            "vehicles": [{
                "vehicle_id": "V", "arrival_slot": 0, "departure_slot": 2,
                "max_charge_wh": [1, 1], "required_energy_wh": 2}],
        }
        offers = {
            "station_id": "ST-X", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30,
            "offers": [{"offer_id": "K", "start_slot": 0, "end_slot": 2,
                        "reduction_wh": 1, "reward": 4}],
        }
        r = plan(fleet, offers)  # 常态可行，不抛异常
        self.assertEqual(r["decision"]["accepted_offer_ids"], [])
        d = r["offer_decisions"][0]
        self.assertFalse(d["feasible_alone"])
        self.assertEqual(d["reject_reason"], "infeasible_alone")
        # 常态计划照常发布且满足需求
        audit_result(r, fleet, offers)


class InvalidInputTests(unittest.TestCase):

    def setUp(self):
        self.fleet = load("fleet-headline.json")
        self.offers = load("offers-headline.json")

    def _expect_invalid(self, fleet=None, offers=None):
        with self.assertRaises(PlanningError) as ctx:
            plan(fleet or self.fleet, offers or self.offers)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_reduction_exceeding_normal_capacity_is_invalid(self):
        self._expect_invalid(offers=load("offers-illegal-reduction.json"))

    def test_station_mismatch(self):
        o = copy.deepcopy(self.offers)
        o["station_id"] = "ST-OTHER"
        self._expect_invalid(offers=o)

    def test_window_mismatch(self):
        o = copy.deepcopy(self.offers)
        o["window_start"] = "2026-12-01T01:00:00Z"
        self._expect_invalid(offers=o)

    def test_slot_minutes_mismatch(self):
        o = copy.deepcopy(self.offers)
        o["slot_minutes"] = 15
        self._expect_invalid(offers=o)

    def test_duplicate_offer_id(self):
        o = copy.deepcopy(self.offers)
        o["offers"][1]["offer_id"] = o["offers"][0]["offer_id"]
        self._expect_invalid(offers=o)

    def test_duplicate_vehicle_id(self):
        f = copy.deepcopy(self.fleet)
        f["vehicles"][1]["vehicle_id"] = f["vehicles"][0]["vehicle_id"]
        self._expect_invalid(offers=self.offers, fleet=f)

    def test_non_positive_reward_and_reduction(self):
        o = copy.deepcopy(self.offers)
        o["offers"][0]["reward"] = 0
        self._expect_invalid(offers=o)
        o2 = copy.deepcopy(self.offers)
        o2["offers"][0]["reduction_wh"] = 0
        self._expect_invalid(offers=o2)

    def test_empty_or_out_of_range_event_window(self):
        o = copy.deepcopy(self.offers)
        o["offers"][0]["start_slot"] = o["offers"][0]["end_slot"]
        self._expect_invalid(offers=o)
        o2 = copy.deepcopy(self.offers)
        o2["offers"][0]["end_slot"] = 99
        self._expect_invalid(offers=o2)

    def test_vehicle_window_and_max_charge_length(self):
        f = copy.deepcopy(self.fleet)
        f["vehicles"][0]["departure_slot"] = f["vehicles"][0]["arrival_slot"]
        self._expect_invalid(fleet=f)
        f2 = copy.deepcopy(self.fleet)
        f2["vehicles"][0]["max_charge_wh"] = [1, 1]  # 长度与时段数不符
        self._expect_invalid(fleet=f2)

    def test_bool_is_not_integer_and_unknown_field_rejected(self):
        o = copy.deepcopy(self.offers)
        o["offers"][0]["reward"] = True
        self._expect_invalid(offers=o)
        f = copy.deepcopy(self.fleet)
        f["bogus"] = 1
        self._expect_invalid(fleet=f)

    def test_naive_timestamp_rejected(self):
        f = copy.deepcopy(self.fleet)
        f["window_start"] = "2026-12-01T00:00:00"
        self._expect_invalid(fleet=f)


class SearchLimitTests(unittest.TestCase):

    def test_too_small_node_budget_raises_distinct_code_not_bad_plan(self):
        fleet = load("fleet-headline.json")
        offers = load("offers-headline.json")
        # 预算 0：第一个 DFS 节点即超限，必须抛可区分错误，绝不返回残缺计划。
        with self.assertRaises(PlanningError) as ctx:
            plan(fleet, offers, search_limit=0)
        self.assertEqual(ctx.exception.code, "search_limit_exceeded")
        # 给足预算后同一输入正常求解。
        r = plan(fleet, offers, search_limit=1_000_000)
        self.assertEqual(r["decision"]["accepted_offer_ids"], ["O2"])


class EdgeCaseTests(unittest.TestCase):

    def test_zero_energy_vehicle_is_satisfied_with_zero_charge(self):
        fleet = {
            "station_id": "ST-Z", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30, "normal_capacity_wh": [1, 1],
            "vehicles": [
                {"vehicle_id": "IDLE", "arrival_slot": 0, "departure_slot": 2,
                 "max_charge_wh": [1, 1], "required_energy_wh": 0},
                {"vehicle_id": "WORK", "arrival_slot": 0, "departure_slot": 2,
                 "max_charge_wh": [1, 1], "required_energy_wh": 1},
            ],
        }
        offers = {"station_id": "ST-Z", "window_start": "2026-12-01T00:00:00Z",
                  "slot_minutes": 30, "offers": []}
        r = plan(fleet, offers)
        audit_result(r, fleet, offers)
        idle = next(v for v in r["scenarios"][0]["vehicles"]
                    if v["vehicle_id"] == "IDLE")
        self.assertEqual(idle["planned_energy_wh"], 0)
        self.assertEqual(idle["charge_wh_by_slot"], [0, 0])

    def test_offer_activating_at_window_start_has_no_locked_prefix(self):
        # start_slot=0：通知在窗口起点到达，没有任何已安排前缀，可整体重排。
        fleet = {
            "station_id": "ST-Z0", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30, "normal_capacity_wh": [2, 2],
            "vehicles": [{"vehicle_id": "V", "arrival_slot": 0, "departure_slot": 2,
                          "max_charge_wh": [2, 2], "required_energy_wh": 3}],
        }
        offers = {"station_id": "ST-Z0", "window_start": "2026-12-01T00:00:00Z",
                  "slot_minutes": 30,
                  "offers": [{"offer_id": "Z", "start_slot": 0, "end_slot": 1,
                              "reduction_wh": 1, "reward": 6}]}
        r = plan(fleet, offers)
        self.assertEqual(r["decision"]["accepted_offer_ids"], ["Z"])
        cont = scenario(r, "offer:Z")
        self.assertEqual(cont["prefix_locked_through_slot"], 0)
        # t0 有效额度 1、t1 为 2；3 Wh 需求可满足，且总量守恒由审计复核。
        audit_result(r, fleet, offers)

    def test_accepted_ids_are_sorted_and_reward_sums_match(self):
        fleet = {
            "station_id": "ST-T2", "window_start": "2026-12-01T00:00:00Z",
            "slot_minutes": 30, "normal_capacity_wh": [3, 3, 3],
            "vehicles": [{"vehicle_id": "V", "arrival_slot": 0, "departure_slot": 3,
                          "max_charge_wh": [1, 1, 1], "required_energy_wh": 1}],
        }
        offers = {"station_id": "ST-T2",
                  "window_start": "2026-12-01T00:00:00Z", "slot_minutes": 30,
                  "offers": [
                      {"offer_id": "B", "start_slot": 2, "end_slot": 3,
                       "reduction_wh": 1, "reward": 5},
                      {"offer_id": "A", "start_slot": 2, "end_slot": 3,
                       "reduction_wh": 1, "reward": 5},
                      {"offer_id": "C", "start_slot": 2, "end_slot": 3,
                       "reduction_wh": 1, "reward": 5},
                  ]}
        r = plan(fleet, offers)
        ids = r["decision"]["accepted_offer_ids"]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(r["decision"]["total_reward"], 5 * len(ids))
        audit_result(r, fleet, offers)


class DeterminismTests(unittest.TestCase):

    def test_shuffled_array_order_does_not_change_decision_or_allocation(self):
        fleet = load("fleet-combo.json")
        offers = load("offers-combo.json")
        baseline = json.dumps(plan(fleet, offers), sort_keys=True)

        f2 = copy.deepcopy(fleet)
        o2 = copy.deepcopy(offers)
        f2["vehicles"].reverse()
        o2["offers"].reverse()
        shuffled = json.dumps(plan(f2, o2), sort_keys=True)
        self.assertEqual(baseline, shuffled)

    def test_retry_with_identical_content_is_byte_stable(self):
        fleet = load("fleet-headline.json")
        offers = load("offers-headline.json")
        a = json.dumps(plan(fleet, offers), sort_keys=True)
        b = json.dumps(plan(copy.deepcopy(fleet), copy.deepcopy(offers)),
                        sort_keys=True)
        self.assertEqual(a, b)


class CliEndToEndTests(unittest.TestCase):

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "charging.planning.cli", *args],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)

    def test_cli_success_writes_result_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "plan.json"
            r = self._cli("--fleet", str(FIX / "fleet-headline.json"),
                          "--offers", str(FIX / "offers-headline.json"),
                          "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["decision"]["accepted_offer_ids"], ["O2"])
            summary = json.loads(r.stdout)
            self.assertEqual(summary["total_reward"], 9)

    def test_cli_invalid_input_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "plan.json"
            good = self._cli("--fleet", str(FIX / "fleet-headline.json"),
                             "--offers", str(FIX / "offers-headline.json"),
                             "--output", str(out))
            self.assertEqual(good.returncode, 0, good.stderr)
            before = out.read_text()

            bad = self._cli("--fleet", str(FIX / "fleet-headline.json"),
                            "--offers", str(FIX / "offers-illegal-reduction.json"),
                            "--output", str(out))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("invalid_input", bad.stderr)
            self.assertEqual(out.read_text(), before)  # 原结果文件保留

    def test_cli_baseline_infeasible_is_distinct_and_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "plan.json"
            good = self._cli("--fleet", str(FIX / "fleet-headline.json"),
                             "--offers", str(FIX / "offers-empty.json"),
                             "--output", str(out))
            self.assertEqual(good.returncode, 0, good.stderr)
            before = out.read_text()

            bad = self._cli("--fleet", str(FIX / "fleet-infeasible.json"),
                            "--offers", str(FIX / "offers-infeasible.json"),
                            "--output", str(out))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("baseline_infeasible", bad.stderr)
            self.assertNotIn("invalid_input", bad.stderr)
            self.assertEqual(out.read_text(), before)

    def test_cli_missing_file_fails_without_touching_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "plan.json"
            r = self._cli("--fleet", str(tmp / "nope.json"),
                          "--offers", str(FIX / "offers-headline.json"),
                          "--output", str(out))
            self.assertEqual(r.returncode, 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
