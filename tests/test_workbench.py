"""收款核销 / 充电准入工作台测试。

覆盖：
* 题面联动验收（含分两轮检验退单失效 + 非法 begin，以及同轮回滚）
* 与真实 charging.engine.apply_batch 产出的 closed_lines/adjustments 联动
* 快照缺失/身份变化拒绝、负应收、宽限边界、恰等于限额、许可过期
* 跨客户误用、过额/重复部分退单、后续 allocate、操作身份冲突与重试
* 跨轮/跨进程恢复、CLI 多轮子进程、金融守恒与既有静态/连续模式不回归
"""

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from charging.engine import apply_batch
from charging.workbench import STATE_VERSION, WorkbenchError, apply_round
from charging.workbench.engine import initial_state

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DUE = "2026-01-31T00:00:00+00:00"


def source_state(lines, adjustments=None):
    """构造一个与 apply_batch 输出同形状的最小结算 state。"""
    closed = {}
    for lid, charge in lines.items():
        session_id = "S-" + lid
        closed[lid] = {
            "line_id": lid, "session_id": session_id, "period_id": "P1",
            "charge_cents": charge, "reflected_charge_cents": charge,
        }
    return {"closed_lines": closed, "adjustments": adjustments or []}


def import_block(lines, adjustments=None, account="A1", due=DUE, period="P1"):
    sessions = {f"S-{lid}": account for lid in lines}
    return {
        "source_state": source_state(lines, adjustments),
        "session_accounts": sessions,
        "period_due_at": {period: due},
    }


def policy(account="A1", limit=30, grace=0):
    return {"account_id": account, "debt_limit_cents": limit, "grace_seconds": grace}


def round_input(round_id, evaluation_at, import_block_=None, policies=None,
                finance=None, service=None):
    out = {"round_id": round_id, "evaluation_at": evaluation_at,
           "finance_operations": finance or [],
           "service_operations": service or []}
    if import_block_ is not None:
        out["import"] = import_block_
    if policies is not None:
        out["register_policies"] = policies
    return out


def line_view(result, line_id):
    return next(x for x in result["lines"] if x["line_id"] == line_id)


def account_view(result, account_id):
    return next(x for x in result["accounts"] if x["account_id"] == account_id)


def permit(result, permit_id):
    return next(x for x in result["permits"] if x["permit_id"] == permit_id)


def receipt_of(result, operation_id):
    return next(x for x in result["receipts_this_round"]
                if x["operation_id"] == operation_id)


class HeadlineAcceptanceTests(unittest.TestCase):
    """题面联动验收全链路。"""

    def _imp(self):
        return import_block({"L1": 100})

    def test_full_admission_lifecycle_and_replay(self):
        # r1: 已到期 100 应收、限额 30；收 60 核销 -> 欠款 40 -> 拒绝许可
        s, res = apply_round(None, round_input(
            "r1", "2026-02-10T00:00:00+00:00", self._imp(), [policy()],
            finance=[{"op": "receive", "operation_id": "op-rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 60,
                      "allocations": [{"line_id": "L1", "amount_cents": 60}]}],
            service=[{"op": "grant", "operation_id": "op-g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        self.assertEqual(account_view(res, "A1")["overdue_debt_cents"], 40)
        denial = receipt_of(res, "op-g1")
        self.assertFalse(denial["granted"])
        self.assertEqual(denial["denied_reason"], "debt_limit_exceeded")
        self.assertEqual(res["permits"], [])

        # r2: 再收 20 -> 欠款 20 -> 签发 G
        s, res = apply_round(s, round_input(
            "r2", "2026-02-11T00:00:00+00:00", self._imp(),
            finance=[{"op": "receive", "operation_id": "op-rec2", "receipt_id": "R2",
                      "account_id": "A1", "amount_cents": 20,
                      "allocations": [{"line_id": "L1", "amount_cents": 20}]}],
            service=[{"op": "grant", "operation_id": "op-g2", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        self.assertEqual(permit(res, "G")["status"], "issued")
        self.assertEqual(line_view(res, "L1")["debt_cents"], 20)

        # r3: 撤回第二笔 20 -> 欠款 40，G 在单独一轮立即失效
        s, res = apply_round(s, round_input(
            "r3", "2026-02-12T00:00:00+00:00", self._imp(),
            finance=[{"op": "chargeback", "operation_id": "op-cb2", "receipt_id": "R2",
                      "withdrawals": [{"line_id": "L1", "amount_cents": 20}]}]))
        self.assertEqual(res["invalidated_this_round"],
                         [{"permit_id": "G", "reason": "debt_limit_exceeded",
                           "at": "2026-02-12T00:00:00+00:00"}])
        self.assertEqual(permit(res, "G")["status"], "invalidated")
        self.assertEqual(permit(res, "G")["invalidated_reason"], "debt_limit_exceeded")
        self.assertEqual(line_view(res, "L1")["debt_cents"], 40)
        snapshot_after_r3 = deepcopy(s)

        # r4: 单独一轮 begin G -> 失败，状态不变
        with self.assertRaisesRegex(WorkbenchError, "permit_not_usable"):
            apply_round(s, round_input(
                "r4", "2026-02-13T00:00:00+00:00", self._imp(),
                service=[{"op": "begin", "operation_id": "op-bad-begin",
                          "permit_id": "G", "station_id": "ST1", "session_id": "SES1"}]))
        self.assertEqual(s, snapshot_after_r3)

        # r5: 退单 + 非法 begin 放同一轮 -> 整轮失败，父状态金额与 G 状态保留
        with self.assertRaisesRegex(WorkbenchError, "permit_not_usable"):
            apply_round(s, round_input(
                "r5", "2026-02-14T00:00:00+00:00", self._imp(),
                finance=[{"op": "chargeback", "operation_id": "op-cb1", "receipt_id": "R1",
                          "withdrawals": [{"line_id": "L1", "amount_cents": 10}]}],
                service=[{"op": "begin", "operation_id": "op-bad-begin2",
                          "permit_id": "G", "station_id": "ST1", "session_id": "SES2"}]))
        self.assertEqual(s, snapshot_after_r3)

        # r6: 再补 20 -> 旧 G 仍失效；新 G2 可签发并启动
        s, res = apply_round(s, round_input(
            "r6", "2026-02-15T00:00:00+00:00", self._imp(),
            finance=[{"op": "receive", "operation_id": "op-rec3", "receipt_id": "R3",
                      "account_id": "A1", "amount_cents": 20,
                      "allocations": [{"line_id": "L1", "amount_cents": 20}]}],
            service=[
                {"op": "grant", "operation_id": "op-g3", "permit_id": "G2",
                 "account_id": "A1", "station_id": "ST1",
                 "expires_at": "2026-03-01T00:00:00+00:00"},
                {"op": "begin", "operation_id": "op-b2", "permit_id": "G2",
                 "station_id": "ST1", "session_id": "SES9"}]))
        self.assertEqual(permit(res, "G")["status"], "invalidated")
        self.assertEqual(permit(res, "G2")["status"], "started")
        self.assertEqual(permit(res, "G2")["session_id"], "SES9")

        # r7: G2 启动后撤回补款 -> 仍 started 并能 finish
        s, res = apply_round(s, round_input(
            "r7", "2026-02-16T00:00:00+00:00", self._imp(),
            finance=[{"op": "chargeback", "operation_id": "op-cb3", "receipt_id": "R3",
                      "withdrawals": [{"line_id": "L1", "amount_cents": 20}]}],
            service=[{"op": "finish", "operation_id": "op-f2",
                      "permit_id": "G2", "session_id": "SES9"}]))
        self.assertEqual(permit(res, "G2")["status"], "finished")
        # started 会话事实没有被轮末欠款反推
        self.assertEqual(permit(res, "G2")["session_id"], "SES9")

        # r8: 重试原 G 的签发请求 op-g2 -> 原签发回执 + 当前 invalidated，不复活
        s, res = apply_round(s, round_input(
            "r8", "2026-02-17T00:00:00+00:00", self._imp(),
            service=[{"op": "grant", "operation_id": "op-g2", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        replay = receipt_of(res, "op-g2")
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["granted"])  # 当时存下的业务回执
        self.assertEqual(permit(res, "G")["status"], "invalidated")  # 当前状态分开输出
        self.assertEqual(len(res["permits"]), 2)  # 没有第二张许可

    def test_denied_grant_retry_after_payment_keeps_denial_receipt(self):
        s, res = apply_round(None, round_input(
            "r1", "2026-02-10T00:00:00+00:00", import_block({"L1": 100}), [policy()],
            service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        self.assertFalse(receipt_of(res, "g1")["granted"])
        # 后来时间重试同一请求：仍是原拒绝回执，不创建许可
        s, res = apply_round(s, round_input(
            "r2", "2026-02-11T00:00:00+00:00", import_block({"L1": 100}),
            service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        self.assertTrue(receipt_of(res, "g1")["replayed"])
        self.assertFalse(receipt_of(res, "g1")["granted"])
        self.assertEqual(res["permits"], [])


class CredentialImportTests(unittest.TestCase):

    def test_adjustment_creates_credit_balance_without_moving_writeoff(self):
        snap1 = import_block({"L1": 100})
        s, res = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap1, [policy(limit=0)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 80,
                      "allocations": [{"line_id": "L1", "amount_cents": 80}]}]))
        self.assertEqual(line_view(res, "L1")["debt_cents"], 20)

        snap2 = import_block({"L1": 100}, [
            {"adjustment_id": "ADJ1", "line_id": "L1", "amount_cents": -50, "period_id": "P2"}])
        s, res = apply_round(s, round_input(
            "r2", "2026-02-02T00:00:00+00:00", snap2))
        lv = line_view(res, "L1")
        self.assertEqual((lv["receivable_cents"], lv["writeoff_cents"],
                          lv["debt_cents"], lv["credit_cents"]), (50, 80, 0, 30))

        # 按原收款撤回 40 -> 有效核销 40、欠款 10，原款归属保留
        s, res = apply_round(s, round_input(
            "r3", "2026-02-03T00:00:00+00:00", snap2,
            finance=[{"op": "chargeback", "operation_id": "cb1", "receipt_id": "R1",
                      "withdrawals": [{"line_id": "L1", "amount_cents": 40}]}]))
        lv = line_view(res, "L1")
        self.assertEqual((lv["writeoff_cents"], lv["debt_cents"]), (40, 10))
        rv = next(x for x in res["receipts"] if x["receipt_id"] == "R1")
        self.assertTrue(rv["conservation"]["balanced"])

    def test_reflected_charge_change_is_not_a_bill_change(self):
        snap1 = import_block({"L1": 100})
        s, _ = apply_round(None, round_input("r1", "2026-02-01T00:00:00+00:00", snap1,
                                             [policy(limit=0)]))
        snap2 = {
            "source_state": {
                "closed_lines": {"L1": {"line_id": "L1", "session_id": "S-L1",
                                        "period_id": "P1", "charge_cents": 100,
                                        "reflected_charge_cents": 42}},
                "adjustments": []},
            "session_accounts": {"S-L1": "A1"},
            "period_due_at": {"P1": DUE},
        }
        s, res = apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00", snap2))
        self.assertEqual(line_view(res, "L1")["receivable_cents"], 100)

    def test_missing_old_credential_and_identity_changes_are_rejected(self):
        snap1 = import_block({"L1": 100})
        s, _ = apply_round(None, round_input("r1", "2026-02-01T00:00:00+00:00", snap1,
                                             [policy(limit=0)]))
        before = deepcopy(s)

        with self.assertRaisesRegex(WorkbenchError, "missing_credential"):
            apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00",
                                       import_block({})))
        with self.assertRaisesRegex(WorkbenchError, "credential_conflict"):
            apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00",
                                       import_block({"L1": 99})))
        # 先建一条 adjustment 再改其金额；adjustment 身份变化必须拒绝
        s2, _ = apply_round(s, round_input(
            "r2", "2026-02-02T00:00:00+00:00",
            import_block({"L1": 100}, [
                {"adjustment_id": "ADJ1", "line_id": "L1", "amount_cents": -5}])))
        with self.assertRaisesRegex(WorkbenchError, "credential_conflict"):
            apply_round(s2, round_input(
                "r3", "2026-02-03T00:00:00+00:00",
                import_block({"L1": 100}, [
                    {"adjustment_id": "ADJ1", "line_id": "L1", "amount_cents": -6}])))
        self.assertEqual(s, before)

    def test_unknown_adjustment_line_reference_rejects_whole_round(self):
        with self.assertRaisesRegex(WorkbenchError, "unknown_line_reference"):
            apply_round(None, round_input(
                "r1", "2026-02-01T00:00:00+00:00",
                import_block({"L1": 100}, [
                    {"adjustment_id": "ADJX", "line_id": "GHOST", "amount_cents": -5}]),
                [policy(limit=0)]))

    def test_new_credential_without_mapping_rejected_and_mapping_is_fixed(self):
        with self.assertRaisesRegex(WorkbenchError, "missing_mapping"):
            apply_round(None, round_input(
                "r1", "2026-02-01T00:00:00+00:00",
                {"source_state": source_state({"L1": 100}),
                 "session_accounts": {}, "period_due_at": {"P1": DUE}},
                [policy(limit=0)]))
        snap = import_block({"L1": 100})
        s, _ = apply_round(None, round_input("r1", "2026-02-01T00:00:00+00:00", snap,
                                             [policy(limit=0)]))
        # 既有客户映射不可改
        conflict = dict(snap)
        conflict["session_accounts"] = {"S-L1": "A2"}
        with self.assertRaisesRegex(WorkbenchError, "credential_conflict"):
            apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00", conflict))

    def test_duplicate_snapshot_does_not_double_count(self):
        snap = import_block({"L1": 100}, [
            {"adjustment_id": "ADJ1", "line_id": "L1", "amount_cents": -10}])
        s, r1 = apply_round(None, round_input("r1", "2026-02-01T00:00:00+00:00", snap,
                                              [policy(limit=0)]))
        s, r2 = apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00", snap))
        self.assertEqual(line_view(r1, "L1")["receivable_cents"], 90)
        self.assertEqual(line_view(r2, "L1")["receivable_cents"], 90)
        self.assertEqual(len(s["adjustments"]), 1)

    def test_new_line_and_adjustment_accumulate_on_original_line(self):
        snap1 = import_block({"L1": 100})
        s, _ = apply_round(None, round_input("r1", "2026-02-01T00:00:00+00:00", snap1,
                                             [policy(limit=0)]))
        snap2 = import_block({"L1": 100, "L2": 50}, [
            {"adjustment_id": "ADJ1", "line_id": "L1", "amount_cents": -20}])
        s, res = apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00", snap2))
        self.assertEqual(line_view(res, "L1")["receivable_cents"], 80)
        self.assertEqual(line_view(res, "L2")["receivable_cents"], 50)


class FinanceConservationTests(unittest.TestCase):

    def test_receive_allocate_chargeback_conservation_and_unallocated_withdrawal(self):
        snap = import_block({"L1": 100, "L2": 40})
        s, res = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=1000)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 100,
                      "allocations": [{"line_id": "L1", "amount_cents": 30}]}]))
        rv = next(x for x in res["receipts"] if x["receipt_id"] == "R1")
        self.assertEqual((rv["effective_writeoff_cents"], rv["unallocated_cents"]), (30, 70))

        # 后续 allocate 只能用该收款的未分配额
        s, res = apply_round(s, round_input(
            "r2", "2026-02-02T00:00:00+00:00", snap,
            finance=[{"op": "allocate", "operation_id": "al1", "receipt_id": "R1",
                      "allocations": [{"line_id": "L1", "amount_cents": 20},
                                      {"line_id": "L2", "amount_cents": 50}]}]))
        rv = next(x for x in res["receipts"] if x["receipt_id"] == "R1")
        self.assertEqual((rv["effective_writeoff_cents"], rv["unallocated_cents"]), (100, 0))
        self.assertEqual(line_view(res, "L1")["writeoff_cents"], 50)

        with self.assertRaisesRegex(WorkbenchError, "over_allocation"):
            apply_round(s, round_input(
                "r3", "2026-02-03T00:00:00+00:00", snap,
                finance=[{"op": "allocate", "operation_id": "al-bad", "receipt_id": "R1",
                          "allocations": [{"line_id": "L2", "amount_cents": 1}]}]))

        # 部分退单：逐单撤回 + 未分配撤回；撤回资金不能再 allocate
        s, res = apply_round(s, round_input(
            "r3", "2026-02-03T00:00:00+00:00", snap,
            finance=[{"op": "receive", "operation_id": "rec2", "receipt_id": "R2",
                      "account_id": "A1", "amount_cents": 60}]))
        s, res = apply_round(s, round_input(
            "r4", "2026-02-04T00:00:00+00:00", snap,
            finance=[{"op": "chargeback", "operation_id": "cb1", "receipt_id": "R2",
                      "unallocated_cents": 25}]))
        rv2 = next(x for x in res["receipts"] if x["receipt_id"] == "R2")
        self.assertEqual((rv2["unallocated_cents"],
                          rv2["withdrawn_unallocated_cents"],
                          rv2["chargeback_total_cents"]), (35, 25, 25))
        with self.assertRaisesRegex(WorkbenchError, "over_allocation"):
            apply_round(s, round_input(
                "r5", "2026-02-05T00:00:00+00:00", snap,
                finance=[{"op": "allocate", "operation_id": "al2", "receipt_id": "R2",
                          "allocations": [{"line_id": "L2", "amount_cents": 36}]}]))

        # 过额退单（超过仍有效核销）拒绝
        with self.assertRaisesRegex(WorkbenchError, "over_chargeback"):
            apply_round(s, round_input(
                "r5", "2026-02-05T00:00:00+00:00", snap,
                finance=[{"op": "chargeback", "operation_id": "cb-bad", "receipt_id": "R1",
                          "withdrawals": [{"line_id": "L1", "amount_cents": 51}]}]))
        # 多次部分退单累计不得超限
        s, _ = apply_round(s, round_input(
            "r5", "2026-02-05T00:00:00+00:00", snap,
            finance=[{"op": "chargeback", "operation_id": "cb2", "receipt_id": "R1",
                      "withdrawals": [{"line_id": "L1", "amount_cents": 25}]}]))
        with self.assertRaisesRegex(WorkbenchError, "over_chargeback"):
            apply_round(s, round_input(
                "r6", "2026-02-06T00:00:00+00:00", snap,
                finance=[{"op": "chargeback", "operation_id": "cb3", "receipt_id": "R1",
                          "withdrawals": [{"line_id": "L1", "amount_cents": 26}]}]))

    def test_duplicate_receipt_and_cross_account_misuse_rejected(self):
        snap = import_block({"L1": 100}, account="A1")
        snap_b = import_block({"LB": 100}, account="A2")
        snap_b["source_state"]["closed_lines"].update(snap["source_state"]["closed_lines"])
        snap_b["session_accounts"].update(snap["session_accounts"])
        s, _ = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap_b,
            [policy("A1", 1000), policy("A2", 1000)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 100}]))
        # 第二次 receive 同一 receipt_id
        with self.assertRaisesRegex(WorkbenchError, "duplicate_receipt"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap_b,
                finance=[{"op": "receive", "operation_id": "rec2", "receipt_id": "R1",
                          "account_id": "A1", "amount_cents": 5}]))
        # A1 的收款不能核销 A2 的单
        with self.assertRaisesRegex(WorkbenchError, "cross_account"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap_b,
                finance=[{"op": "allocate", "operation_id": "alx", "receipt_id": "R1",
                          "allocations": [{"line_id": "LB", "amount_cents": 5}]}]))
        # chargeback 不能引用别人的收款（未知收款）
        with self.assertRaisesRegex(WorkbenchError, "unknown_receipt"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap_b,
                finance=[{"op": "chargeback", "operation_id": "cbx", "receipt_id": "GHOST",
                          "unallocated_cents": 1}]))

    def test_negative_receivable_kept_as_credit_and_not_offset_against_other_lines(self):
        snap = import_block({"L1": 100, "L2": 100}, [
            {"adjustment_id": "ADJ1", "line_id": "L1", "amount_cents": -150}])
        s, res = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=0)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 100,
                      "allocations": [{"line_id": "L1", "amount_cents": 100}]}]))
        l1, l2 = line_view(res, "L1"), line_view(res, "L2")
        self.assertEqual(l1["receivable_cents"], -50)
        self.assertEqual((l1["credit_cents"], l1["debt_cents"]), (150, 0))
        # 贷方不自动抵其他单：L2 仍是 100 欠款
        self.assertEqual((l2["receivable_cents"], l2["debt_cents"], l2["credit_cents"]),
                         (100, 100, 0))

    def test_replay_of_finance_operation_creates_no_second_movement(self):
        snap = import_block({"L1": 100})
        op = {"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
              "account_id": "A1", "amount_cents": 40,
              "allocations": [{"line_id": "L1", "amount_cents": 40}]}
        s, r1 = apply_round(None, round_input("r1", "2026-02-01T00:00:00+00:00", snap,
                                              [policy(limit=1000)], finance=[op]))
        finance_before = deepcopy(s)
        s, r2 = apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00", snap,
                                           finance=[deepcopy(op)]))
        # 金融事实无变化（仅时钟推进）
        for key in ("lines", "adjustments", "receipts", "permits", "operations"):
            self.assertEqual(s[key], finance_before[key])
        self.assertTrue(receipt_of(r2, "rec1")["replayed"])
        self.assertEqual(len(s["receipts"]), 1)

    def test_operation_id_content_conflict_rejects_round(self):
        snap = import_block({"L1": 100})
        s, _ = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=1000)],
            finance=[{"op": "receive", "operation_id": "op1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 40}]))
        before = deepcopy(s)
        # 同 operation_id 不同载荷
        with self.assertRaisesRegex(WorkbenchError, "operation_conflict"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap,
                finance=[{"op": "receive", "operation_id": "op1", "receipt_id": "R1",
                          "account_id": "A1", "amount_cents": 41}]))
        # 同 operation_id 换操作种类
        with self.assertRaisesRegex(WorkbenchError, "operation_conflict"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap,
                finance=[{"op": "allocate", "operation_id": "op1", "receipt_id": "R1",
                          "allocations": [{"line_id": "L1", "amount_cents": 1}]}]))
        self.assertEqual(s, before)

    def test_zero_change_operation_rejected(self):
        snap = import_block({"L1": 100})
        s, _ = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=1000)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 40}]))
        with self.assertRaisesRegex(WorkbenchError, "invalid_operation"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap,
                finance=[{"op": "chargeback", "operation_id": "cb0", "receipt_id": "R1",
                          "unallocated_cents": 0, "withdrawals": []}]))


class PolicyAndAdmissionTests(unittest.TestCase):

    def test_policy_required_immutable_and_reusable(self):
        snap = import_block({"L1": 100})
        with self.assertRaisesRegex(WorkbenchError, "unknown_policy"):
            apply_round(None, round_input(
                "r1", "2026-02-01T00:00:00+00:00", snap,
                service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                          "account_id": "A1", "station_id": "ST1",
                          "expires_at": "2026-03-01T00:00:00+00:00"}]))
        s, _ = apply_round(None, round_input("r1", "2026-02-01T00:00:00+00:00", snap,
                                             [policy(limit=30, grace=0)]))
        # 同内容重复登记可复用
        s, _ = apply_round(s, round_input("r2", "2026-02-02T00:00:00+00:00", snap,
                                          [policy(limit=30, grace=0)]))
        # 修订被拒绝
        with self.assertRaisesRegex(WorkbenchError, "policy_conflict"):
            apply_round(s, round_input("r3", "2026-02-03T00:00:00+00:00", snap,
                                       [policy(limit=31, grace=0)]))

    def test_grace_boundary_and_exactly_at_limit(self):
        # due 2026-01-31T00:00, grace 86400s -> 截止 2026-02-01T00:00
        snap = import_block({"L1": 100}, due=DUE)
        base = round_input("r1", "2026-02-01T00:00:00+00:00", snap,
                           [policy(limit=100, grace=86400)])
        s, res = apply_round(None, base)
        # evaluation_at == due+grace：严格晚于才算逾期 -> 逾期欠款 0，准入合格
        self.assertEqual(account_view(res, "A1")["overdue_debt_cents"], 0)
        self.assertEqual(account_view(res, "A1")["admission"], "qualified")

        # 恰等于限额：欠款严格大于限额才拒绝 -> 100 == 100 合格
        s, res = apply_round(s, round_input(
            "r2", "2026-02-01T00:00:01+00:00", snap,
            service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        self.assertTrue(receipt_of(res, "g1")["granted"])
        self.assertEqual(permit(res, "G")["status"], "issued")

    def test_multiple_due_dates_aggregate_only_overdue_positive_debt(self):
        snap = import_block({"L1": 100}, due="2026-01-31T00:00:00+00:00")
        # 加一张更晚到期的单
        snap["source_state"]["closed_lines"]["L2"] = {
            "line_id": "L2", "session_id": "S-L2", "period_id": "P2", "charge_cents": 70}
        snap["session_accounts"]["S-L2"] = "A1"
        snap["period_due_at"]["P2"] = "2026-03-31T00:00:00+00:00"
        s, res = apply_round(None, round_input(
            "r1", "2026-02-15T00:00:00+00:00", snap, [policy(limit=50, grace=0)]))
        view = account_view(res, "A1")
        self.assertEqual(view["overdue_debt_cents"], 100)  # L2 未到期不计
        self.assertEqual(view["overdue_line_ids"], ["L1"])
        self.assertEqual(view["admission"], "debt_limit_exceeded")

    def test_clock_cannot_move_backwards(self):
        snap = import_block({"L1": 100})
        s, _ = apply_round(None, round_input("r1", "2026-02-02T00:00:00+00:00", snap,
                                             [policy(limit=1000)]))
        with self.assertRaisesRegex(WorkbenchError, "clock_moved_backwards"):
            apply_round(s, round_input("r2", "2026-02-01T23:59:59+00:00", snap))

    def test_naive_timestamp_rejected(self):
        with self.assertRaisesRegex(WorkbenchError, "invalid_round"):
            apply_round(None, {"round_id": "r1", "evaluation_at": "2026-02-01T00:00:00",
                               "finance_operations": [], "service_operations": []})


class PermitLifecycleTests(unittest.TestCase):

    def _state_with_permit(self, expires="2026-03-01T00:00:00+00:00", limit=1000):
        snap = import_block({"L1": 100})
        s, res = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=limit)],
            service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1", "expires_at": expires}]))
        return s, snap, res

    def test_grant_expires_at_must_be_future(self):
        snap = import_block({"L1": 100})
        with self.assertRaisesRegex(WorkbenchError, "permit_expired"):
            apply_round(None, round_input(
                "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=1000)],
                service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                          "account_id": "A1", "station_id": "ST1",
                          "expires_at": "2026-02-01T00:00:00+00:00"}]))

    def test_begin_requires_right_station_valid_permit_and_unique_session(self):
        s, snap, _ = self._state_with_permit()
        with self.assertRaisesRegex(WorkbenchError, "station_mismatch"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap,
                service=[{"op": "begin", "operation_id": "b1", "permit_id": "G",
                          "station_id": "ST2", "session_id": "SES1"}]))
        s, res = apply_round(s, round_input(
            "r2", "2026-02-02T00:00:00+00:00", snap,
            service=[{"op": "begin", "operation_id": "b1", "permit_id": "G",
                      "station_id": "ST1", "session_id": "SES1"}]))
        self.assertEqual(permit(res, "G")["status"], "started")
        # 一个许可只能启动一个会话
        with self.assertRaisesRegex(WorkbenchError, "permit_not_usable"):
            apply_round(s, round_input(
                "r3", "2026-02-03T00:00:00+00:00", snap,
                service=[{"op": "begin", "operation_id": "b2", "permit_id": "G",
                          "station_id": "ST1", "session_id": "SES2"}]))
        # 一个 session_id 不能由另一许可再次使用
        s, _ = apply_round(s, round_input(
            "r3", "2026-02-03T00:00:00+00:00", snap,
            service=[{"op": "grant", "operation_id": "g2", "permit_id": "G2",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-10T00:00:00+00:00"}]))
        with self.assertRaisesRegex(WorkbenchError, "session_conflict"):
            apply_round(s, round_input(
                "r4", "2026-02-04T00:00:00+00:00", snap,
                service=[{"op": "begin", "operation_id": "b3", "permit_id": "G2",
                          "station_id": "ST1", "session_id": "SES1"}]))

    def test_expired_unstarted_permit_invalidated_with_distinct_reason(self):
        s, snap, _ = self._state_with_permit(expires="2026-02-05T00:00:00+00:00")
        s, res = apply_round(s, round_input("r2", "2026-02-05T00:00:00+00:00", snap))
        self.assertEqual(permit(res, "G")["status"], "invalidated")
        self.assertEqual(permit(res, "G")["invalidated_reason"], "expired")
        # 过期后 begin 失败，原因可区分
        with self.assertRaisesRegex(WorkbenchError, "permit_not_usable"):
            apply_round(s, round_input(
                "r3", "2026-02-06T00:00:00+00:00", snap,
                service=[{"op": "begin", "operation_id": "b1", "permit_id": "G",
                          "station_id": "ST1", "session_id": "SES1"}]))

    def test_finish_requires_actual_started_session_and_is_terminal(self):
        s, snap, _ = self._state_with_permit()
        # 未启动不能 finish
        with self.assertRaisesRegex(WorkbenchError, "permit_not_started"):
            apply_round(s, round_input(
                "r2", "2026-02-02T00:00:00+00:00", snap,
                service=[{"op": "finish", "operation_id": "f0", "permit_id": "G",
                          "session_id": "SES1"}]))
        s, _ = apply_round(s, round_input(
            "r2", "2026-02-02T00:00:00+00:00", snap,
            service=[{"op": "begin", "operation_id": "b1", "permit_id": "G",
                      "station_id": "ST1", "session_id": "SES1"}]))
        # finish 必须指向实际启动的会话
        with self.assertRaisesRegex(WorkbenchError, "session_mismatch"):
            apply_round(s, round_input(
                "r3", "2026-02-03T00:00:00+00:00", snap,
                service=[{"op": "finish", "operation_id": "f1", "permit_id": "G",
                          "session_id": "OTHER"}]))
        s, res = apply_round(s, round_input(
            "r3", "2026-02-03T00:00:00+00:00", snap,
            service=[{"op": "finish", "operation_id": "f1", "permit_id": "G",
                      "session_id": "SES1"}]))
        self.assertEqual(permit(res, "G")["status"], "finished")
        with self.assertRaisesRegex(WorkbenchError, "permit_not_started"):
            apply_round(s, round_input(
                "r4", "2026-02-04T00:00:00+00:00", snap,
                service=[{"op": "finish", "operation_id": "f2", "permit_id": "G",
                          "session_id": "SES1"}]))

    def test_disqualified_begin_in_same_round_rejected_and_started_survives(self):
        # 两张单：L2 全额收款保持合格（限额 150：r1/r2 逾期欠款均为 100），
        # G 签发并启动、G2 签发；退单后总欠款 200 > 150 才失格。
        snap = import_block({"L1": 100, "L2": 100})
        s, _ = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=150)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 100,
                      "allocations": [{"line_id": "L2", "amount_cents": 100}]}],
            service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"},
                     {"op": "begin", "operation_id": "b1", "permit_id": "G",
                      "station_id": "ST1", "session_id": "SES1"}]))
        # G2 在合格时签发，保持 issued
        s, _ = apply_round(s, round_input(
            "r2", "2026-02-02T00:00:00+00:00", snap,
            service=[{"op": "grant", "operation_id": "g2", "permit_id": "G2",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        before = deepcopy(s)
        # 同轮：退单恢复 L2 的 100 欠款（轮末逾期 200），并尝试 begin G2。
        # 阶段4先把未启动的 G2 标记失效，阶段5 begin 因许可已失效被拒，整轮回滚。
        with self.assertRaisesRegex(WorkbenchError, "permit_not_usable|debt_not_qualified"):
            apply_round(s, round_input(
                "r3", "2026-02-03T00:00:00+00:00", snap,
                finance=[{"op": "chargeback", "operation_id": "cb1", "receipt_id": "R1",
                          "withdrawals": [{"line_id": "L2", "amount_cents": 100}]}],
                service=[{"op": "begin", "operation_id": "b2", "permit_id": "G2",
                          "station_id": "ST1", "session_id": "SES2"}]))
        self.assertEqual(s, before)  # 回滚：退单与失效均不落库

        # 分开两轮：先退单 -> G2 被失效（原因可区分），G 保持 started
        s2, res2 = apply_round(before, round_input(
            "r3b", "2026-02-03T00:00:00+00:00", snap,
            finance=[{"op": "chargeback", "operation_id": "cb1", "receipt_id": "R1",
                      "withdrawals": [{"line_id": "L2", "amount_cents": 100}]}]))
        self.assertEqual(permit(res2, "G")["status"], "started")
        self.assertEqual(permit(res2, "G2")["status"], "invalidated")
        self.assertEqual(permit(res2, "G2")["invalidated_reason"],
                         "debt_limit_exceeded")
        # 失格客户的 begin 不得成功（许可已失效，原因可辨别）
        with self.assertRaisesRegex(WorkbenchError, "permit_not_usable"):
            apply_round(s2, round_input(
                "r4", "2026-02-04T00:00:00+00:00", snap,
                service=[{"op": "begin", "operation_id": "b3", "permit_id": "G2",
                          "station_id": "ST1", "session_id": "SES3"}]))
        # started 会话仍可结束
        s2, res2 = apply_round(s2, round_input(
            "r5", "2026-02-05T00:00:00+00:00", snap,
            service=[{"op": "finish", "operation_id": "f1", "permit_id": "G",
                      "session_id": "SES1"}]))
        self.assertEqual(permit(res2, "G")["status"], "finished")

    def test_begin_directly_rejects_when_debt_disqualified_while_permit_still_issued(self):
        # 直接覆盖 begin 内部的 debt_not_qualified 分支：手工构造一张仍 issued、
        # 未过期、站点正确、session 未占用，但账户轮末欠款超限额的状态。
        from datetime import datetime
        from charging.workbench import engine as wb_engine
        snap = import_block({"L1": 100})
        s, _ = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=10, grace=0)],
            service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"}]))
        # 上面的 grant 因欠款 100 > 10 被拒，手工放入一张 issued 许可，
        # 模拟“许可在本轮阶段4之前仍有效”的判定点
        s["permits"]["G"] = {
            "permit_id": "G", "account_id": "A1", "station_id": "ST1",
            "expires_at": "2026-03-01T00:00:00+00:00",
            "issued_at": "2026-02-01T00:00:00+00:00", "status": "issued",
            "invalidated_reason": None, "invalidated_at": None,
            "session_id": None, "started_at": None, "finished_at": None}
        evaluation_at = datetime.fromisoformat("2026-02-02T00:00:00+00:00")
        adj_index = wb_engine._adjustments_by_line(s)
        op = {"op": "begin", "operation_id": "b-direct", "permit_id": "G",
              "station_id": "ST1", "session_id": "SES-DIRECT"}
        with self.assertRaisesRegex(WorkbenchError, "debt_not_qualified"):
            wb_engine._do_begin(s, op, "r-direct", evaluation_at, adj_index)
        # 被拒后没有产生会话、许可仍 issued
        self.assertNotIn("SES-DIRECT", s["used_sessions"])
        self.assertEqual(s["permits"]["G"]["status"], "issued")


class SettlementEngineIntegrationTests(unittest.TestCase):
    """真实调用 charging.engine.apply_batch 后再导入工作台。"""

    def _real_settlement_state(self):
        from tests.test_continuous import (TARIFFS_FLAT, P1, P2,
                                           balanced_base_batch, close, rec)
        state, _ = apply_batch(None, balanced_base_batch())
        state, _ = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2]})
        # 更正使冻结 10 分单变为 0 -> 产生 -10 adjustment
        state, res = apply_batch(state, {"batch_id": "b3", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200,
                "r-st1-1")]})
        self.assertIn("P1:S1", state["closed_lines"])
        self.assertEqual(state["closed_lines"]["P1:S1"]["charge_cents"], 10)
        self.assertEqual(state["adjustments"][0]["amount_cents"], -10)
        return state

    def test_import_real_apply_batch_state_and_writeoff_flow(self):
        settlement = self._real_settlement_state()
        imp = {
            "source_state": settlement,
            "session_accounts": {"S1": "A1"},
            "period_due_at": {"P1": "2026-01-31T00:00:00+00:00"},
        }
        s, res = apply_round(None, round_input(
            "r1", "2026-02-10T00:00:00+00:00", imp, [policy(limit=0)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 8,
                      "allocations": [{"line_id": "P1:S1", "amount_cents": 8}]}]))
        # 应收 = 10 + (-10) = 0；核销 8 -> 贷方 8、欠款 0
        lv = line_view(res, "P1:S1")
        self.assertEqual((lv["receivable_cents"], lv["debt_cents"], lv["credit_cents"]),
                         (0, 0, 8))
        # 输入结算 state 不被修改
        self.assertEqual(settlement["closed_lines"]["P1:S1"]["charge_cents"], 10)

    def test_import_only_reads_listed_credential_fields(self):
        settlement = self._real_settlement_state()
        imp = {
            "source_state": settlement,
            "session_accounts": {"S1": "A1"},
            "period_due_at": {"P1": "2026-01-31T00:00:00+00:00"},
        }
        s, r1 = apply_round(None, round_input("r1", "2026-02-10T00:00:00+00:00", imp,
                                              [policy(limit=0)]))
        # 再导入时即便结算侧重放带来额外内部字段，工作台身份比较仍稳定
        s2, r2 = apply_round(s, round_input("r2", "2026-02-11T00:00:00+00:00", imp))
        self.assertEqual(line_view(r1, "P1:S1"), line_view(r2, "P1:S1"))


class RecoveryTests(unittest.TestCase):

    def test_json_roundtrip_preserves_receipts_sessions_and_invalidation(self):
        snap = import_block({"L1": 100})
        s, _ = apply_round(None, round_input(
            "r1", "2026-02-01T00:00:00+00:00", snap, [policy(limit=30)],
            finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                      "account_id": "A1", "amount_cents": 100,
                      "allocations": [{"line_id": "L1", "amount_cents": 100}]}],
            service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                      "account_id": "A1", "station_id": "ST1",
                      "expires_at": "2026-03-01T00:00:00+00:00"},
                     {"op": "begin", "operation_id": "b1", "permit_id": "G",
                      "station_id": "ST1", "session_id": "SES1"}]))
        # 退单制造失格，但 started 会话保留
        s, res = apply_round(s, round_input(
            "r2", "2026-02-02T00:00:00+00:00", snap,
            finance=[{"op": "chargeback", "operation_id": "cb1", "receipt_id": "R1",
                      "withdrawals": [{"line_id": "L1", "amount_cents": 100}]}]))
        self.assertEqual(permit(res, "G")["status"], "started")

        restored = json.loads(json.dumps(s))
        self.assertEqual(restored["version"], STATE_VERSION)
        s2, res2 = apply_round(restored, round_input(
            "r3", "2026-02-03T00:00:00+00:00", snap,
            service=[{"op": "finish", "operation_id": "f1", "permit_id": "G",
                      "session_id": "SES1"}]))
        self.assertEqual(permit(res2, "G")["status"], "finished")
        # 旧回执仍可重放
        s2, res3 = apply_round(s2, round_input(
            "r4", "2026-02-04T00:00:00+00:00", snap,
            service=[{"op": "begin", "operation_id": "b1", "permit_id": "G",
                      "station_id": "ST1", "session_id": "SES1"}]))
        self.assertTrue(receipt_of(res3, "b1")["replayed"])
        # 守恒在恢复后仍然成立
        for rv in res2["receipts"]:
            self.assertTrue(rv["conservation"]["balanced"])


class CliEndToEndTests(unittest.TestCase):

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "charging.workbench.cli", *args],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)

    def test_multi_round_cli_with_cross_process_recovery_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "wb-state.json"
            result_path = tmp / "wb-result.json"
            settlement_path = tmp / "settlement.json"
            settlement_path.write_text(json.dumps(source_state({"L1": 100})))

            def write_round(name, payload):
                p = tmp / name
                p.write_text(json.dumps(payload))
                return p

            def run(name, payload, expect_ok=True):
                rp = write_round(name, payload)
                r = self._cli("--round", str(rp), "--state", str(state_path),
                              "--output-state", str(state_path),
                              "--output", str(result_path),
                              "--source-state", str(settlement_path))
                if expect_ok:
                    self.assertEqual(r.returncode, 0, r.stderr)
                else:
                    self.assertEqual(r.returncode, 2)
                return r

            imp_ref = {"source_state_ref": str(settlement_path),
                       "session_accounts": {"S-L1": "A1"},
                       "period_due_at": {"P1": DUE}}

            # r1: 收 60 核销、欠款 40、G 被拒
            r1 = run("r1.json", round_input(
                "r1", "2026-02-10T00:00:00+00:00", imp_ref, [policy()],
                finance=[{"op": "receive", "operation_id": "rec1", "receipt_id": "R1",
                          "account_id": "A1", "amount_cents": 60,
                          "allocations": [{"line_id": "L1", "amount_cents": 60}]}],
                service=[{"op": "grant", "operation_id": "g1", "permit_id": "G",
                          "account_id": "A1", "station_id": "ST1",
                          "expires_at": "2026-03-01T00:00:00+00:00"}]))
            self.assertEqual(json.loads(r1.stdout)["grant_denied"], ["G"])
            result = json.loads(result_path.read_text())
            self.assertFalse(receipt_of(result, "g1")["granted"])

            # r2: 再收 20 -> G 签发
            r2 = run("r2.json", round_input(
                "r2", "2026-02-11T00:00:00+00:00", imp_ref,
                finance=[{"op": "receive", "operation_id": "rec2", "receipt_id": "R2",
                          "account_id": "A1", "amount_cents": 20,
                          "allocations": [{"line_id": "L1", "amount_cents": 20}]}],
                service=[{"op": "grant", "operation_id": "g2", "permit_id": "G",
                          "account_id": "A1", "station_id": "ST1",
                          "expires_at": "2026-03-01T00:00:00+00:00"}]))
            self.assertEqual(json.loads(r2.stdout)["granted"], ["G"])
            state_before_cb = state_path.read_text()

            # r3: 退单 + 非法 begin 同轮 -> 退出 2，状态与结果文件保持父轮内容
            bad = run("r3.json", round_input(
                "r3", "2026-02-12T00:00:00+00:00", imp_ref,
                finance=[{"op": "chargeback", "operation_id": "cb2", "receipt_id": "R2",
                          "withdrawals": [{"line_id": "L1", "amount_cents": 20}]}],
                service=[{"op": "begin", "operation_id": "b-bad", "permit_id": "G",
                          "station_id": "ST1", "session_id": "SES1"}]), expect_ok=False)
            self.assertIn("permit_not_usable", bad.stderr)
            self.assertEqual(state_path.read_text(), state_before_cb)

            # r4: 仅退单 -> G 失效（跨进程恢复后金融守恒与许可状态保持一致）
            run("r4.json", round_input(
                "r4", "2026-02-12T00:00:00+00:00", imp_ref,
                finance=[{"op": "chargeback", "operation_id": "cb2", "receipt_id": "R2",
                          "withdrawals": [{"line_id": "L1", "amount_cents": 20}]}]))
            result = json.loads(result_path.read_text())
            self.assertEqual(result["invalidated_this_round"][0]["permit_id"], "G")
            for rv in result["receipts"]:
                self.assertTrue(rv["conservation"]["balanced"])

            # 结算 state 文件从未被工作台改写
            self.assertEqual(json.loads(settlement_path.read_text()),
                             source_state({"L1": 100}))

    def test_cli_missing_state_creates_fresh_workbench(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "fresh.json"
            result_path = tmp / "result.json"
            rp = tmp / "r1.json"
            rp.write_text(json.dumps(round_input(
                "r1", "2026-02-01T00:00:00+00:00", None, [policy(limit=1000)])))
            r = self._cli("--round", str(rp), "--state", str(state_path),
                          "--output-state", str(state_path), "--output", str(result_path))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(state_path.exists())
            self.assertEqual(json.loads(state_path.read_text())["version"], STATE_VERSION)


if __name__ == "__main__":
    unittest.main()
