import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from charging.core import settle
from charging.engine import BatchError, STATE_VERSION, apply_batch

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TARIFFS_FLAT = [
    {"start": "2026-01-01T00:00:00+00:00", "end": "2026-02-01T00:00:00+00:00", "cents_per_kwh": 10},
    {"start": "2026-02-01T00:00:00+00:00", "end": "2026-03-01T00:00:00+00:00", "cents_per_kwh": 10},
    {"start": "2026-03-01T00:00:00+00:00", "end": "2026-04-01T00:00:00+00:00", "cents_per_kwh": 10},
]
TARIFFS_BOUNDARY = [
    {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-10T00:30:00+00:00", "cents_per_kwh": 20},
    {"start": "2026-01-10T00:30:00+00:00", "end": "2026-02-01T00:00:00+00:00", "cents_per_kwh": 30},
]

P1 = {"period_id": "P1", "start": "2026-01-01T00:00:00+00:00",
      "end": "2026-02-01T00:00:00+00:00", "action": "open"}
P2 = {"period_id": "P2", "start": "2026-02-01T00:00:00+00:00",
      "end": "2026-03-01T00:00:00+00:00", "action": "open"}
P3 = {"period_id": "P3", "start": "2026-03-01T00:00:00+00:00",
      "end": "2026-04-01T00:00:00+00:00", "action": "open"}


def rec(rid, source, instant, wh, supersedes=None):
    out = {"record_id": rid, "source_id": source, "meter_instant": instant, "cumulative_wh": wh}
    if supersedes is not None:
        out["supersedes_record_id"] = supersedes
    return out


def close(period):
    p = dict(period)
    p["action"] = "close"
    return p


def balanced_base_batch(batch_id="b1", tariffs=None, periods=("P1",), sessions=("S1",), wh=1000):
    """Station import and session meter both run 0 -> ``wh`` across one hour."""
    tariffs = tariffs or TARIFFS_FLAT
    period_by_id = {"P1": P1, "P2": P2, "P3": P3}
    stations = [{"station_id": "ST1", "import_source_id": "M-ST1"}]
    sess_defs = []
    records = [
        rec("r-st1-0", "M-ST1", "2026-01-01T00:00:00+00:00", 0),
        rec("r-st1-1", "M-ST1", "2026-01-10T01:00:00+00:00", wh * len(sessions)),
    ]
    for i, sid in enumerate(sessions):
        meter = f"M-{sid}"
        base = i * wh
        sess_defs.append({
            "session_id": sid, "station_id": "ST1", "meter_source_id": meter,
            "start": "2026-01-10T00:00:00+00:00", "end": "2026-01-10T01:00:00+00:00",
        })
        records.append(rec(f"r-{sid}-0", meter, "2026-01-10T00:00:00+00:00", base))
        records.append(rec(f"r-{sid}-1", meter, "2026-01-10T01:00:00+00:00", base + wh))
    return {
        "batch_id": batch_id,
        "tariffs": tariffs,
        "stations": stations,
        "sessions": sess_defs,
        "records": records,
        "periods": [dict(period_by_id[p]) for p in periods],
    }


class CounterexampleTests(unittest.TestCase):
    """The headline scenario from the specification."""

    def test_station_correction_to_1200_creates_residual_withheld_and_negative_adjustment(self):
        state, res = apply_batch(None, balanced_base_batch())
        self.assertEqual(res["totals"]["current_charge_cents"], 10)
        self.assertTrue(res["session_charges"][0]["session_id"] == "S1")

        state, res = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2]})
        self.assertEqual(res["closed_statements"][0]["total_charge_cents"], 10)

        correction = {"batch_id": "b3", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]}
        state, res = apply_batch(state, correction)

        # station evidence: 200 Wh unexplained, session loses billable status
        self.assertEqual(res["station_residuals"][0]["residual_wh"], 200)
        self.assertEqual(res["station_residuals"][0]["station_import_wh"], 1200)
        self.assertEqual(res["station_residuals"][0]["session_meter_wh"], 1000)
        withheld = next(w for w in res["withheld_energy"] if w["session_id"] == "S1")
        self.assertFalse(withheld["billable"])
        self.assertEqual(withheld["withheld_wh"], 1000)
        self.assertEqual(withheld["station_residual_wh"], 200)
        self.assertEqual(res["session_charges"], [])

        # frozen line untouched, signed adjustment in next open period
        frozen = state["closed_lines"]["P1:S1"]
        self.assertEqual(frozen["charge_cents"], 10)
        stmt = next(s for s in res["closed_statements"] if s["period_id"] == "P1")
        self.assertEqual(stmt["lines"][0]["charge_cents"], 10)
        adj = res["next_period_adjustments"][0]
        self.assertEqual(adj["amount_cents"], -10)
        self.assertEqual(adj["period_id"], "P2")
        self.assertEqual(adj["line_id"], "P1:S1")
        self.assertEqual(adj["correction_record_ids"], ["r-st1-1-c1"])
        self.assertEqual(adj["batch_id"], "b3")

        # both sides visible separately in the state
        self.assertEqual(res["totals"]["residual_wh"], 200)
        self.assertEqual(res["totals"]["withheld_wh"], 1000)
        self.assertEqual(res["totals"]["adjustment_cents"], -10)


class CorrectionChainTests(unittest.TestCase):

    def _closed_two_period_state(self):
        state, _ = apply_batch(None, balanced_base_batch())
        state, _ = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2]})
        return state

    def test_reverse_correction_rebalances_and_posts_positive_adjustment(self):
        state = self._closed_two_period_state()
        state, res = apply_batch(state, {"batch_id": "b3", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]})
        self.assertEqual(res["totals"]["residual_wh"], 200)

        state, res = apply_batch(state, {"batch_id": "b4", "records": [
            rec("r-st1-1-c2", "M-ST1", "2026-01-10T01:00:00+00:00", 1000, "r-st1-1-c1")]})
        self.assertEqual(res["station_residuals"][0]["residual_wh"], 0)
        self.assertTrue(next(w for w in res["withheld_energy"] if w["session_id"] == "S1")["billable"])
        self.assertEqual([a["amount_cents"] for a in res["next_period_adjustments"]], [-10, 10])
        # frozen statement never changed
        self.assertEqual(state["closed_lines"]["P1:S1"]["charge_cents"], 10)

    def test_middle_reading_correction_changes_both_adjacent_intervals(self):
        batch = balanced_base_batch()
        # add a midpoint reading on both meters: intervals [00:00,00:30] and [00:30,01:00]
        batch["records"] += [
            rec("r-st1-m", "M-ST1", "2026-01-10T00:30:00+00:00", 500),
            rec("r-S1-m", "M-S1", "2026-01-10T00:30:00+00:00", 500),
        ]
        state, res = apply_batch(None, batch)
        self.assertEqual([iv["residual_wh"] for iv in res["station_residuals"]], [0, 0])

        # correct the middle station reading 500 -> 700
        state, res = apply_batch(state, {"batch_id": "b-mid", "records": [
            rec("r-st1-m-c1", "M-ST1", "2026-01-10T00:30:00+00:00", 700, "r-st1-m")]})
        intervals = sorted(res["station_residuals"], key=lambda iv: iv["start"])
        # first interval grows by 200 (700-500), second shrinks by 200 (300-500)
        self.assertEqual([iv["station_import_wh"] for iv in intervals], [700, 300])
        self.assertEqual([iv["residual_wh"] for iv in intervals], [200, -200])
        # both sides changed, and withheld/residual stay separate fields
        withheld = next(w for w in res["withheld_energy"] if w["session_id"] == "S1")
        self.assertEqual(withheld["withheld_wh"], 1000)
        self.assertEqual(withheld["station_residual_wh"], 0)  # signed residuals net to zero

    def test_out_of_order_chain_in_one_batch_resolves_to_same_head(self):
        head = rec("r-st1-1-c2", "M-ST1", "2026-01-10T01:00:00+00:00", 1100, "r-st1-1-c1")
        middle = rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")
        s_chrono, _ = apply_batch(None, balanced_base_batch(batch_id="base"))
        s_chrono, _ = apply_batch(s_chrono, {"batch_id": "c1", "records": [middle], "periods": []})
        s_chrono, _ = apply_batch(s_chrono, {"batch_id": "c2", "records": [head], "periods": []})

        shuffled = balanced_base_batch(batch_id="all-at-once")
        shuffled["records"] += [head, middle]  # head submitted before its predecessor
        s_shuffled, res_shuffled = apply_batch(None, shuffled)

        station_head = next(c for c in s_shuffled["chains"]
                            if c["source_id"] == "M-ST1" and "01:00" in c["meter_instant"])
        self.assertEqual(station_head["head_record_id"], "r-st1-1-c2")
        # effective heads identical regardless of record order inside the batch
        def heads(state):
            return {(c["source_id"], c["meter_instant"]): c["head_record_id"] for c in state["chains"]}
        self.assertEqual(heads(s_shuffled), heads(s_chrono))
        self.assertEqual(res_shuffled["station_residuals"][0]["residual_wh"], 100)

    def test_shuffled_multi_batch_matches_chronological(self):
        # chronological: bA then bB; shuffled submission of the same two batches
        a = {"batch_id": "bA", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")],
            "periods": []}
        b = {"batch_id": "bB", "records": [
            rec("r-st1-1-c2", "M-ST1", "2026-01-10T01:00:00+00:00", 1050, "r-st1-1-c1")],
            "periods": []}
        s_chrono, r_chrono = apply_batch(None, balanced_base_batch())
        s_chrono, r_chrono = apply_batch(s_chrono, a)
        s_chrono, r_chrono = apply_batch(s_chrono, b)

        base = balanced_base_batch()
        base["batch_id"] = "b-base"
        s_sh, r_sh = apply_batch(None, base)
        with self.assertRaisesRegex(BatchError, "unknown_predecessor"):
            # bB first: predecessor not yet present -> rejected, nothing staged
            apply_batch(s_sh, b)
        # parent state survived the rejection
        s_sh2, r_sh2 = apply_batch(s_sh, a)
        s_sh2, r_sh2 = apply_batch(s_sh2, b)
        self.assertEqual(s_sh2["chains"], s_chrono["chains"])
        self.assertEqual(r_sh2["station_residuals"], r_chrono["station_residuals"])

    def test_unknown_predecessor_is_rejected_and_not_staged(self):
        state, _ = apply_batch(None, balanced_base_batch())
        bad = {"batch_id": "bad", "records": [
            rec("x1", "M-ST1", "2026-01-10T01:00:00+00:00", 9999, "does-not-exist")]}
        with self.assertRaisesRegex(BatchError, "unknown_predecessor"):
            apply_batch(state, bad)
        # state must not contain the rejected record
        self.assertNotIn("x1", {r["record_id"] for r in state["records"]})

    def test_duplicate_record_id_rejected(self):
        state, _ = apply_batch(None, balanced_base_batch())
        bad = {"batch_id": "dup", "records": [
            rec("r-st1-1", "M-ST1", "2026-01-10T01:00:00+00:00", 1234)]}
        with self.assertRaisesRegex(BatchError, "duplicate_record_id"):
            apply_batch(state, bad)
        with self.assertRaisesRegex(BatchError, "duplicate_record_id"):
            apply_batch(None, {"batch_id": "dup-in-batch", "records": [
                rec("same", "M-S1", "2026-01-10T00:00:00+00:00", 1),
                rec("same", "M-S1", "2026-01-10T00:00:00+00:00", 2)]})

    def test_cycle_and_branching_heads_rejected(self):
        # pure cycle in a fresh slot: every record is superseded, no head exists
        with self.assertRaisesRegex(BatchError, "cyclic_chain"):
            apply_batch(None, {
                "batch_id": "cyclic",
                "tariffs": TARIFFS_FLAT,
                "stations": [{"station_id": "ST1", "import_source_id": "M-ST1"}],
                "sessions": [{
                    "session_id": "S1", "station_id": "ST1", "meter_source_id": "M-S1",
                    "start": "2026-01-10T00:00:00+00:00", "end": "2026-01-10T01:00:00+00:00"}],
                "records": [
                    rec("r-st1-0", "M-ST1", "2026-01-01T00:00:00+00:00", 0),
                    rec("r-st1-1", "M-ST1", "2026-01-10T01:00:00+00:00", 1000),
                    rec("r-S1-0", "M-S1", "2026-01-10T00:00:00+00:00", 0),
                    rec("cy1", "M-S1", "2026-01-10T01:00:00+00:00", 1001, "cy3"),
                    rec("cy2", "M-S1", "2026-01-10T01:00:00+00:00", 1002, "cy1"),
                    rec("cy3", "M-S1", "2026-01-10T01:00:00+00:00", 1003, "cy2"),
                ],
                "periods": [dict(P1)]})

        # a self-contained cycle arriving in a later batch is likewise rejected
        state, _ = apply_batch(None, balanced_base_batch())
        with self.assertRaisesRegex(BatchError, "cyclic_chain"):
            apply_batch(state, {"batch_id": "cy-late", "records": [
                rec("cy-a", "M-ST1", "2026-01-15T00:00:00+00:00", 1, "cy-c"),
                rec("cy-b", "M-ST1", "2026-01-15T00:00:00+00:00", 2, "cy-a"),
                rec("cy-c", "M-ST1", "2026-01-15T00:00:00+00:00", 3, "cy-b")]})
        self.assertNotIn("cy-a", {r["record_id"] for r in state["records"]})

        # two records superseding the same predecessor -> conflicting head / branch
        branch_base = balanced_base_batch(batch_id="branch-base")
        with self.assertRaisesRegex(BatchError, "branching_chain|conflicting_head"):
            apply_batch(None, {
                "batch_id": "branch",
                "tariffs": TARIFFS_FLAT,
                "stations": branch_base["stations"],
                "sessions": branch_base["sessions"],
                "records": [
                    rec("r-st1-0", "M-ST1", "2026-01-01T00:00:00+00:00", 0),
                    rec("r-st1-1", "M-ST1", "2026-01-10T01:00:00+00:00", 1000),
                    rec("r-S1-0", "M-S1", "2026-01-10T00:00:00+00:00", 0),
                    rec("r-S1-1", "M-S1", "2026-01-10T01:00:00+00:00", 1000),
                    rec("br1", "M-S1", "2026-01-10T01:00:00+00:00", 1001, "r-S1-1"),
                    rec("br2", "M-S1", "2026-01-10T01:00:00+00:00", 1002, "r-S1-1"),
                ],
                "periods": [dict(P1)]})

    def test_foreign_slot_supersede_rejected(self):
        state, _ = apply_batch(None, balanced_base_batch())
        with self.assertRaisesRegex(BatchError, "unknown_predecessor"):
            apply_batch(state, {"batch_id": "foreign", "records": [
                rec("f1", "M-ST1", "2026-01-10T01:00:00+00:00", 1, "r-S1-1")]})

    def test_record_id_is_globally_unique_even_across_slots(self):
        state, _ = apply_batch(None, balanced_base_batch())
        with self.assertRaisesRegex(BatchError, "duplicate_record_id"):
            apply_batch(state, {"batch_id": "cross-slot", "records": [
                rec("r-st1-1", "M-ST1", "2026-01-12T00:00:00+00:00", 1)]})

    def test_zero_energy_session_is_withheld_not_billable(self):
        batch = balanced_base_batch()
        # add a balanced second session with zero net Wh
        batch["sessions"].append({
            "session_id": "SZ", "station_id": "ST1", "meter_source_id": "M-SZ",
            "start": "2026-01-11T00:00:00+00:00", "end": "2026-01-11T01:00:00+00:00"})
        batch["records"] += [
            rec("r-SZ-0", "M-SZ", "2026-01-11T00:00:00+00:00", 0),
            rec("r-SZ-1", "M-SZ", "2026-01-11T01:00:00+00:00", 0),
            # station import holds at 1000 Wh across the zero-energy session
            rec("r-st1-2", "M-ST1", "2026-01-11T00:00:00+00:00", 1000),
            rec("r-st1-3", "M-ST1", "2026-01-11T01:00:00+00:00", 1000),
        ]
        _, res = apply_batch(None, batch)
        z = next(w for w in res["withheld_energy"] if w["session_id"] == "SZ")
        self.assertFalse(z["billable"])
        self.assertEqual(z["reason"], "zero_energy")
        self.assertEqual(z["withheld_wh"], 0)
        self.assertEqual([c["session_id"] for c in res["session_charges"]], ["S1"])


class ReplayTests(unittest.TestCase):

    def test_same_batch_id_replay_is_idempotent(self):
        state, res1 = apply_batch(None, balanced_base_batch())
        state, _ = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2]})
        corr = {"batch_id": "b3", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]}
        state, res2 = apply_batch(state, corr)
        snapshot = deepcopy(state)
        state_again, res3 = apply_batch(state, corr)
        self.assertTrue(res3["replayed"])
        self.assertEqual(state_again, snapshot)
        self.assertEqual(len(res3["next_period_adjustments"]), 1)
        self.assertEqual(res3["totals"], res2["totals"])

    def test_replay_with_different_content_under_same_batch_id_rejected(self):
        state, _ = apply_batch(None, balanced_base_batch())
        with self.assertRaisesRegex(BatchError, "batch_replay_conflict"):
            apply_batch(state, {"batch_id": "b1", "records": [], "periods": []})

    def test_identical_correction_record_replayed_in_new_batch_adds_nothing(self):
        state, _ = apply_batch(None, balanced_base_batch())
        state, _ = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2]})
        state, res = apply_batch(state, {"batch_id": "b3", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]})
        before = deepcopy(state)
        # same immutable record id+content again under a new batch id: no-op merge, no adjustment
        state, res2 = apply_batch(state, {"batch_id": "b4", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]})
        self.assertEqual(len(state["adjustments"]), 1)
        self.assertEqual(state["records"], before["records"])
        self.assertEqual(res2["totals"]["adjustment_cents"], -10)

    def test_same_history_sequence_replays_to_identical_state_and_totals(self):
        # running the exact same batch sequence twice from scratch must yield
        # identical states, residuals and adjustment totals
        def run_sequence():
            base = balanced_base_batch(batch_id="base")
            state, _ = apply_batch(None, base)
            state, _ = apply_batch(state, {"batch_id": "close", "periods": [close(P1), P2]})
            state, _ = apply_batch(state, {"batch_id": "c1", "records": [
                rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]})
            state, r2 = apply_batch(state, {"batch_id": "c2", "records": [
                rec("r-st1-1-c2", "M-ST1", "2026-01-10T01:00:00+00:00", 1100, "r-st1-1-c1")]})
            return state, r2

        s_a, r_a = run_sequence()
        s_b, r_b = run_sequence()
        self.assertEqual(s_a, s_b)
        self.assertEqual(r_a["totals"], r_b["totals"])
        self.assertEqual(r_a["next_period_adjustments"], r_b["next_period_adjustments"])

        # a batch carrying the whole correction history before the first close
        # freezes the corrected (unbillable) snapshot instead of adjusting it:
        # same final heads/residuals, adjustments only exist for post-close fixes
        one = balanced_base_batch(batch_id="everything")
        one["periods"] = [dict(P1), close(P1), dict(P2)]
        one["records"] += [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1"),
            rec("r-st1-1-c2", "M-ST1", "2026-01-10T01:00:00+00:00", 1100, "r-st1-1-c1"),
        ]
        s_one, r_one = apply_batch(None, one)
        self.assertEqual(r_one["station_residuals"], r_a["station_residuals"])
        self.assertEqual(
            {c["meter_instant"]: c["head_record_id"] for c in s_one["chains"] if c["source_id"] == "M-ST1"},
            {c["meter_instant"]: c["head_record_id"] for c in s_a["chains"] if c["source_id"] == "M-ST1"})
        self.assertEqual(r_one["next_period_adjustments"], [])
        self.assertEqual(r_one["closed_statements"][0]["lines"][0]["billable"], False)

    def test_invalid_batch_leaves_parent_state_and_outputs_untouched(self):
        state, _ = apply_batch(None, balanced_base_batch())
        state, _ = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2]})
        snapshot = deepcopy(state)
        bad = {"batch_id": "bad", "periods": [
            {"period_id": "P9", "start": "2026-01-15T00:00:00+00:00",
             "end": "2026-01-16T00:00:00+00:00", "action": "close"}]}
        with self.assertRaises(BatchError):
            apply_batch(state, bad)
        self.assertEqual(state, snapshot)
        # invalid record payload likewise changes nothing
        with self.assertRaises(BatchError):
            apply_batch(state, {"batch_id": "bad2", "records": [
                {"record_id": "z", "source_id": "M-ST1", "meter_instant": "not-a-timestamp",
                 "cumulative_wh": 1}]})
        self.assertEqual(state, snapshot)


class PeriodAndAdjustmentTests(unittest.TestCase):

    def test_open_projection_closed_snapshot_and_no_open_period_boundary(self):
        state, res = apply_batch(None, balanced_base_batch())
        # open period projection exists and reflects the current charge
        self.assertEqual(res["periods"][0]["status"], "open")
        self.assertEqual(res["totals"]["current_charge_cents"], 10)

        # close without any successor open period
        with self.assertRaisesRegex(BatchError, "unknown_period"):
            apply_batch(state, {"batch_id": "close-bad", "periods": [close(P2)]})
        state, res = apply_batch(state, {"batch_id": "close", "periods": [close(P1)]})
        self.assertEqual(res["closed_statements"][0]["status"], "closed")
        self.assertEqual(res["closed_statements"][0]["lines"][0]["charge_cents"], 10)

        # correction that changes a frozen amount with no next open period
        with self.assertRaisesRegex(BatchError, "no_open_period"):
            apply_batch(state, {"batch_id": "late", "records": [
                rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]})
        # frozen line unchanged after the rejected batch
        self.assertEqual(state["closed_lines"]["P1:S1"]["charge_cents"], 10)
        self.assertEqual(state["adjustments"], [])

        # a non-impacting correction (same value re-asserted through a chain) is fine even w/o open period
        state2, res2 = apply_batch(state, {"batch_id": "neutral", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1000, "r-st1-1")]})
        self.assertEqual(state2["adjustments"], [])

    def test_period_overlap_and_bounds_rejected(self):
        state, _ = apply_batch(None, balanced_base_batch())
        with self.assertRaisesRegex(BatchError, "period_overlap"):
            apply_batch(state, {"batch_id": "ov", "periods": [
                {"period_id": "PX", "start": "2026-01-15T00:00:00+00:00",
                 "end": "2026-02-15T00:00:00+00:00", "action": "open"}]})

    def test_continuous_batches_restore_full_state(self):
        state, _ = apply_batch(None, balanced_base_batch())
        state, _ = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2]})
        state, _ = apply_batch(state, {"batch_id": "b3", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]})
        state, _ = apply_batch(state, {"batch_id": "b4", "periods": [close(P2), P3]})
        state, res = apply_batch(state, {"batch_id": "b5", "records": [
            rec("r-st1-1-c2", "M-ST1", "2026-01-10T01:00:00+00:00", 1000, "r-st1-1-c1")]})

        # serialize/deserialize round trip preserves everything
        restored, res2 = apply_batch(json.loads(json.dumps(state)),
                                     {"batch_id": "b6", "records": [], "periods": []})
        self.assertEqual(res2["closed_statements"], res["closed_statements"])
        self.assertEqual(res2["next_period_adjustments"], res["next_period_adjustments"])
        self.assertEqual(
            restored["processed_batches"].keys() - {"b6"}, state["processed_batches"].keys())
        self.assertEqual(restored["version"], STATE_VERSION)
        # adjustment traceability: each adjustment names its frozen line and correction identity
        for adj in restored["adjustments"]:
            self.assertIn(adj["line_id"], restored["closed_lines"])
            self.assertTrue(adj["adjustment_id"].startswith(adj["line_id"] + ":"))

    def test_adjustment_targets_earliest_open_successor(self):
        state, _ = apply_batch(None, balanced_base_batch())
        state, _ = apply_batch(state, {"batch_id": "b2", "periods": [close(P1), P2, P3]})
        # P2 stays open; both successors exist, so the earliest one wins
        state, res = apply_batch(state, {"batch_id": "b4", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]})
        adj = res["next_period_adjustments"][0]
        self.assertEqual(adj["source_period_id"], "P1")
        self.assertEqual(adj["period_id"], "P2")  # earliest open successor, not P3

        # once P2 is closed too, a rebalancing correction (+10 cents) rolls into P3
        state, _ = apply_batch(state, {"batch_id": "b5", "periods": [close(P2)]})
        state, res = apply_batch(state, {"batch_id": "b6", "records": [
            rec("r-st1-1-c2", "M-ST1", "2026-01-10T01:00:00+00:00", 1000, "r-st1-1-c1")]})
        self.assertEqual(res["next_period_adjustments"][-1]["period_id"], "P3")
        self.assertEqual(res["next_period_adjustments"][-1]["amount_cents"], 10)


class TariffAndOutputTests(unittest.TestCase):

    def test_tariff_boundary_inside_corrected_interval_keeps_legacy_rules(self):
        batch = balanced_base_batch(tariffs=TARIFFS_BOUNDARY)
        state, res = apply_batch(None, batch)
        charge = res["session_charges"][0]
        # 1000 Wh split evenly by elapsed seconds: 500 @ 20c + 500 @ 30c = 10 + 15 = 25
        self.assertEqual([s["energy_wh"] for s in charge["segments"]], [500, 500])
        self.assertEqual([s["charge_cents"] for s in charge["segments"]], [10, 15])
        self.assertEqual(charge["charge_cents"], 25)

        # correction changes the session meter energy: 1000 -> 1001 Wh with station matching
        state, _ = apply_batch(state, {"batch_id": "corr-s", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1001, "r-st1-1"),
            rec("r-S1-1-c1", "M-S1", "2026-01-10T01:00:00+00:00", 1001, "r-S1-1")]})
        state, res = apply_batch(state, {"batch_id": "view", "records": [], "periods": []})
        charge = res["session_charges"][0]
        # integer allocation: first segment floor(1001*1800/3600)=500, last gets remainder 501
        self.assertEqual([s["energy_wh"] for s in charge["segments"]], [500, 501])
        # half-up per segment: 500*20/1000=10; 501*30/1000=15030/1000 -> 15
        self.assertEqual([s["charge_cents"] for s in charge["segments"]], [10, 15])
        self.assertEqual(sum(s["charge_cents"] for s in charge["segments"]), 25)

    def test_half_up_rounding_matches_legacy_formula(self):
        # 333 Wh @ 10 c/kWh: 3330/1000 -> half-up 3; verify via both engines
        tariffs = [{"start": "2026-01-01T00:00:00+00:00", "end": "2026-02-01T00:00:00+00:00",
                    "cents_per_kwh": 10}]
        legacy = settle({"tariffs": tariffs, "sessions": [{
            "id": "S1", "start": "2026-01-10T00:00:00+00:00",
            "end": "2026-01-10T01:00:00+00:00", "start_wh": 0, "end_wh": 333}]})
        batch = balanced_base_batch(wh=333, tariffs=tariffs + [
            {"start": "2026-02-01T00:00:00+00:00", "end": "2026-03-01T00:00:00+00:00",
             "cents_per_kwh": 10}])
        _, res = apply_batch(None, batch)
        self.assertEqual(res["session_charges"][0]["charge_cents"],
                         legacy["sessions"][0]["charge_cents"])
        self.assertEqual(legacy["sessions"][0]["charge_cents"], 3)

    def test_stable_session_ordering_and_separate_fields(self):
        batch = balanced_base_batch(sessions=("S1", "S2", "S3"))
        state, res = apply_batch(None, batch)
        self.assertEqual([c["session_id"] for c in res["session_charges"]], ["S1", "S2", "S3"])
        self.assertEqual([w["session_id"] for w in res["withheld_energy"]], ["S1", "S2", "S3"])

        # knock station off balance: all sessions withheld, residual and withheld stay separate
        state, res = apply_batch(state, {"batch_id": "off", "records": [
            rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 3500, "r-st1-1")]})
        self.assertEqual(res["session_charges"], [])
        self.assertEqual({w["withheld_wh"] for w in res["withheld_energy"]}, {1000})
        self.assertEqual(res["station_residuals"][0]["residual_wh"], 500)
        self.assertEqual(res["totals"]["withheld_wh"], 3000)
        self.assertEqual(res["totals"]["residual_wh"], 500)

    def test_station_residuals_are_scoped_per_station(self):
        # two independent balanced stations; one goes off-balance without
        # contaminating the other station's residual or billability
        batch = {
            "batch_id": "multi",
            "tariffs": TARIFFS_FLAT,
            "stations": [
                {"station_id": "ST1", "import_source_id": "M-ST1"},
                {"station_id": "ST2", "import_source_id": "M-ST2"}],
            "sessions": [
                {"session_id": "S1", "station_id": "ST1", "meter_source_id": "M-S1",
                 "start": "2026-01-10T00:00:00+00:00", "end": "2026-01-10T01:00:00+00:00"},
                {"session_id": "S2", "station_id": "ST2", "meter_source_id": "M-S2",
                 "start": "2026-01-10T00:00:00+00:00", "end": "2026-01-10T01:00:00+00:00"}],
            "records": [
                rec("r-st1-0", "M-ST1", "2026-01-01T00:00:00+00:00", 0),
                rec("r-st1-1", "M-ST1", "2026-01-10T01:00:00+00:00", 1000),
                rec("r-st2-0", "M-ST2", "2026-01-01T00:00:00+00:00", 0),
                rec("r-st2-1", "M-ST2", "2026-01-10T01:00:00+00:00", 2000),
                rec("r-s1-0", "M-S1", "2026-01-10T00:00:00+00:00", 0),
                rec("r-s1-1", "M-S1", "2026-01-10T01:00:00+00:00", 1000),
                rec("r-s2-0", "M-S2", "2026-01-10T00:00:00+00:00", 0),
                rec("r-s2-1", "M-S2", "2026-01-10T01:00:00+00:00", 2000),
            ],
            "periods": [dict(P1)],
        }
        state, res = apply_batch(None, batch)
        self.assertEqual({iv["station_id"]: iv["residual_wh"]
                          for iv in res["station_residuals"]}, {"ST1": 0, "ST2": 0})
        self.assertEqual([c["session_id"] for c in res["session_charges"]], ["S1", "S2"])

        # ST2 import jumps 2000 -> 2300: only ST2 shows residual, only S2 withheld
        state, res = apply_batch(state, {"batch_id": "multi-corr", "records": [
            rec("r-st2-1-c1", "M-ST2", "2026-01-10T01:00:00+00:00", 2300, "r-st2-1")]})
        by_station = {iv["station_id"]: iv for iv in res["station_residuals"]}
        self.assertEqual(by_station["ST1"]["residual_wh"], 0)
        self.assertEqual(by_station["ST2"]["residual_wh"], 300)
        self.assertEqual(by_station["ST2"]["session_meter_wh"], 2000)
        self.assertEqual([c["session_id"] for c in res["session_charges"]], ["S1"])
        withheld = {w["session_id"]: w for w in res["withheld_energy"]}
        self.assertTrue(withheld["S1"]["billable"])
        self.assertFalse(withheld["S2"]["billable"])
        self.assertEqual(withheld["S2"]["withheld_wh"], 2000)

    def test_adjustment_traces_only_relevant_corrections(self):
        # a batch carrying corrections for two different stations: the
        # adjustment on S1's frozen line may only name S1's own meters
        batch = {
            "batch_id": "base",
            "tariffs": TARIFFS_FLAT,
            "stations": [
                {"station_id": "ST1", "import_source_id": "M-ST1"},
                {"station_id": "ST2", "import_source_id": "M-ST2"}],
            "sessions": [
                {"session_id": "S1", "station_id": "ST1", "meter_source_id": "M-S1",
                 "start": "2026-01-10T00:00:00+00:00", "end": "2026-01-10T01:00:00+00:00"},
                {"session_id": "S2", "station_id": "ST2", "meter_source_id": "M-S2",
                 "start": "2026-01-10T00:00:00+00:00", "end": "2026-01-10T01:00:00+00:00"}],
            "records": [
                rec("a0", "M-ST1", "2026-01-01T00:00:00+00:00", 0),
                rec("a1", "M-ST1", "2026-01-10T01:00:00+00:00", 1000),
                rec("b0", "M-ST2", "2026-01-01T00:00:00+00:00", 0),
                rec("b1", "M-ST2", "2026-01-10T01:00:00+00:00", 500),
                rec("m1a", "M-S1", "2026-01-10T00:00:00+00:00", 0),
                rec("m1b", "M-S1", "2026-01-10T01:00:00+00:00", 1000),
                rec("m2a", "M-S2", "2026-01-10T00:00:00+00:00", 0),
                rec("m2b", "M-S2", "2026-01-10T01:00:00+00:00", 500),
            ],
            "periods": [dict(P1)],
        }
        state, _ = apply_batch(None, batch)
        state, _ = apply_batch(state, {"batch_id": "close", "periods": [close(P1), P2]})
        state, res = apply_batch(state, {"batch_id": "both-corr", "records": [
            rec("a1c", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "a1"),
            rec("b1c", "M-ST2", "2026-01-10T01:00:00+00:00", 700, "b1")]})
        adj_s1 = next(a for a in res["next_period_adjustments"] if a["session_id"] == "S1")
        adj_s2 = next(a for a in res["next_period_adjustments"] if a["session_id"] == "S2")
        self.assertEqual(adj_s1["correction_record_ids"], ["a1c"])
        self.assertEqual(adj_s2["correction_record_ids"], ["b1c"])

    def test_tariff_gap_still_rejected(self):
        with self.assertRaisesRegex(BatchError, "non-overlapping|coverage"):
            apply_batch(None, balanced_base_batch(tariffs=[
                {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-05T00:00:00+00:00",
                 "cents_per_kwh": 10}]))


class LegacyCompatTests(unittest.TestCase):

    def test_legacy_static_fixture_unchanged(self):
        data = json.loads((PROJECT_ROOT / "fixtures" / "input.json").read_text())
        result = settle(data)
        self.assertEqual(result["sessions"][0]["session_id"], "C1")
        self.assertEqual([x["energy_wh"] for x in result["sessions"][0]["segments"]], [1000, 1000])
        self.assertEqual(result["total_charge_cents"], 58)
        self.assertEqual(result["sessions"][1]["session_id"], "C2")

    def test_legacy_rejects_tariff_gap(self):
        data = {"tariffs": [{"start": "2026-01-01T00:00:00+00:00",
                             "end": "2026-01-01T01:00:00+00:00", "cents_per_kwh": 10}],
                "sessions": [{"id": "x", "start": "2026-01-01T00:30:00+00:00",
                              "end": "2026-01-01T02:00:00+00:00", "start_wh": 0, "end_wh": 1000}]}
        with self.assertRaisesRegex(ValueError, "coverage gap"):
            settle(data)


class CliEndToEndTests(unittest.TestCase):

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "charging.cli", *args],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)

    def test_legacy_cli_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "legacy.json"
            r = self._cli("--input", str(PROJECT_ROOT / "fixtures" / "input.json"), "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["total_charge_cents"], 58)
            # legacy mode still refuses to overwrite an existing file
            r2 = self._cli("--input", str(PROJECT_ROOT / "fixtures" / "input.json"), "--output", str(out))
            self.assertNotEqual(r2.returncode, 0)

    def test_full_continuous_flow_via_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state_path = tmp / "state.json"
            result_path = tmp / "result.json"

            # round 1: create state
            b1 = tmp / "b1.json"
            b1.write_text(json.dumps(balanced_base_batch()))
            r = self._cli("--batch", str(b1), "--output-state", str(state_path),
                          "--output", str(result_path))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(state_path.exists())
            summary = json.loads(r.stdout.strip())
            self.assertEqual(summary["current_charge_cents"], 10)

            # round 2: close P1, open P2
            b2 = tmp / "b2.json"
            b2.write_text(json.dumps({"batch_id": "b2", "periods": [close(P1), P2]}))
            r = self._cli("--batch", str(b2), "--state", str(state_path),
                          "--output-state", str(state_path), "--output", str(result_path))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(result_path.read_text())["closed_statements"][0]["total_charge_cents"], 10)

            # round 3: correction 1000 -> 1200
            b3 = tmp / "b3.json"
            b3.write_text(json.dumps({"batch_id": "b3", "records": [
                rec("r-st1-1-c1", "M-ST1", "2026-01-10T01:00:00+00:00", 1200, "r-st1-1")]}))
            r = self._cli("--batch", str(b3), "--state", str(state_path),
                          "--output-state", str(state_path), "--output", str(result_path))
            self.assertEqual(r.returncode, 0, r.stderr)
            result = json.loads(result_path.read_text())
            self.assertEqual(result["totals"]["residual_wh"], 200)
            self.assertEqual(result["next_period_adjustments"][0]["amount_cents"], -10)

            # round 4: replay the same batch -> identical result, state unchanged
            state_before = state_path.read_text()
            r = self._cli("--batch", str(b3), "--state", str(state_path),
                          "--output-state", str(state_path), "--output", str(result_path))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(json.loads(r.stdout.strip())["replayed"])
            self.assertEqual(state_path.read_text(), state_before)

            # round 5: invalid batch fails with exit 2, state and result files untouched
            result_before = result_path.read_text()
            bad = tmp / "bad.json"
            bad.write_text(json.dumps({"batch_id": "bad", "records": [
                rec("x", "M-ST1", "2026-01-10T01:00:00+00:00", 1, "ghost")]}))
            r = self._cli("--batch", str(bad), "--state", str(state_path),
                          "--output-state", str(state_path), "--output", str(result_path))
            self.assertEqual(r.returncode, 2)
            self.assertIn("unknown_predecessor", r.stderr)
            self.assertEqual(state_path.read_text(), state_before)
            self.assertEqual(result_path.read_text(), result_before)


if __name__ == "__main__":
    unittest.main()
