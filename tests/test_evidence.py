"""充电启动证据核验测试。

测试把 ``analyze`` 发布的结果当作外部产物独立复核：

* 题面三项关键行为：两渠道时间歧义（站点 1..1、渠道 0..1、无确证）、
  强证据优先（带码确证不可为凑对数放弃）、繁忙枪口积压（32×32 精确
  16/16 与 0..16，无确证，多解不是错误）；
* 边界：恰好 120 秒、跨枪口、不匹配的关联码、单侧带码、窗外请求凭强
  证据占用窗内请求也能匹配的结果、结果早于请求、0 秒差；
* 证据清单覆盖：确证/未配对/有待人工核查三类恰好覆盖全部记录；
* 端点证据：每份 witness 都是合法且最优的完整配对，成功数与端点一致，
  且包含全部确证配对；
* 目标基点值为整数精确比较（含 0、10000 与恰等于目标的边界）；
* 无数据组、非法输入（退出码 2、保留已有输出）、真实 CLI 调用、
  调换日志行序后输出逐字节一致；
* 与实现完全独立的暴力枚举交叉校验（随机小实例）。
"""

import json
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from charging.evidence import EVIDENCE_VERSION, EvidenceError, analyze

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIX = PROJECT_ROOT / "fixtures" / "evidence"

BASE = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIX / name).read_text())


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def make_req(rid, gun, seconds, channel="mobile", code=None, station="ST-T",
             cutoff_seconds=3600):
    return {
        "station_id": station,
        "observed_until": iso(BASE + timedelta(seconds=cutoff_seconds)),
        "requests": [{
            "request_id": rid, "gun_id": gun,
            "requested_at": iso(BASE + timedelta(seconds=seconds)),
            "channel": channel,
            **({"correlation_code": code} if code is not None else {}),
        }],
    }


def req_doc(requests, station="ST-T", cutoff_seconds=3600):
    return {"station_id": station,
            "observed_until": iso(BASE + timedelta(seconds=cutoff_seconds)),
            "requests": requests}


def res_doc(results, station="ST-T", cutoff_seconds=3600):
    return {"station_id": station,
            "observed_until": iso(BASE + timedelta(seconds=cutoff_seconds)),
            "results": results}


def req(rid, gun, seconds, channel="mobile", code=None):
    d = {"request_id": rid, "gun_id": gun,
         "requested_at": iso(BASE + timedelta(seconds=seconds)),
         "channel": channel}
    if code is not None:
        d["correlation_code"] = code
    return d


def res(rid, gun, seconds, outcome="started", code=None):
    d = {"result_id": rid, "gun_id": gun,
         "resolved_at": iso(BASE + timedelta(seconds=seconds)),
         "outcome": outcome}
    if code is not None:
        d["correlation_code"] = code
    return d


WINDOW = (iso(BASE - timedelta(seconds=60)), iso(BASE + timedelta(seconds=240)))


def run_analyze(requests, results, window=WINDOW, target_bp=5000, **kw):
    return analyze(req_doc(requests, **kw), res_doc(results, **kw),
                   window[0], window[1], target_bp)


# ---------------------------------------------------------------------------
# 独立复核工具
# ---------------------------------------------------------------------------

def parse(dt_str):
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def audit_coverage(result):
    """确证/未配对/有待核查三类恰好覆盖快照中的全部记录（每类记录一次）。"""
    ev = result["evidence"]
    conf_reqs = [p["request_id"] for p in ev["confirmed_pairs"]]
    conf_res = [p["result_id"] for p in ev["confirmed_pairs"]]
    all_reqs = (conf_reqs + [r["request_id"] for r in ev["unmatched"]["requests"]]
                + [r["request_id"] for r in ev["needs_review"]["requests"]])
    all_res = (conf_res + [r["result_id"] for r in ev["unmatched"]["results"]]
               + [r["result_id"] for r in ev["needs_review"]["results"]])
    assert len(all_reqs) == len(set(all_reqs)) == result["matching_summary"]["requests"]
    assert len(all_res) == len(set(all_res)) == result["matching_summary"]["results"]


def audit_witness(result, requests_doc, results_doc, group, tag):
    """独立复核一份端点证据：合法、最优、成功数与端点一致、含全部确证。"""
    groups = result["operations"]["groups"]
    wit = groups[group][f"witness_{tag}"]
    summary = result["matching_summary"]
    reqs = {r["request_id"]: r for r in requests_doc["requests"]}
    res = {r["result_id"]: r for r in results_doc["results"]}
    ws, we = parse(result["window"]["start"]), parse(result["window"]["end"])

    seen_req, seen_res = set(), set()
    strong = 0
    successes = 0
    for p in wit["pairs"]:
        q, s = reqs[p["request_id"]], res[p["result_id"]]
        assert p["request_id"] not in seen_req and p["result_id"] not in seen_res
        seen_req.add(p["request_id"])
        seen_res.add(p["result_id"])
        # 关联规则独立复核
        assert q["gun_id"] == s["gun_id"] == p["gun_id"]
        delta = (parse(s["resolved_at"]) - parse(q["requested_at"])).total_seconds()
        assert 0 <= delta <= 120
        qc, sc = q.get("correlation_code"), s.get("correlation_code")
        assert not (qc and sc and qc != sc)
        expect_strong = bool(qc) and bool(sc)
        assert p["evidence"] == ("strong" if expect_strong else "time")
        strong += expect_strong
        assert p["channel"] == q["channel"]
        assert p["outcome"] == s["outcome"]
        in_window = ws <= parse(q["requested_at"]) < we
        assert p["in_window"] == in_window
        if (in_window and (group == "station" or q["channel"] == group)
                and s["outcome"] == "started"):
            successes += 1
    # 最优性：与汇总口径一致
    assert wit["strong_pairs"] == strong == summary["strong_pairs"]
    assert wit["total_pairs"] == len(wit["pairs"]) == summary["total_pairs"]
    # 成功数与端点一致
    assert wit["successes"] == successes
    assert successes == groups[group][f"successes_{tag}"]
    # 确证配对必须出现在每份最优解释中，因此也在这份证据里
    conf = {(p["request_id"], p["result_id"]) for p in result["evidence"]["confirmed_pairs"]}
    assert conf <= {(p["request_id"], p["result_id"]) for p in wit["pairs"]}


def audit_all_witnesses(result, requests_doc, results_doc):
    for group in ("station", "mobile", "terminal"):
        for tag in ("min", "max"):
            audit_witness(result, requests_doc, results_doc, group, tag)


# ---------------------------------------------------------------------------
# 题面关键行为
# ---------------------------------------------------------------------------

class TwoChannelAmbiguityTests(unittest.TestCase):
    """同一枪口两渠道请求 + 一起动一故障、无关联码：站点 1..1，渠道 0..1。"""

    def setUp(self):
        self.requests = load("two-channel-requests.json")
        self.results = load("two-channel-results.json")
        self.result = analyze(self.requests, self.results,
                              "2026-11-20T10:00:00Z", "2026-11-20T10:02:00Z", 5000)

    def test_summary_and_confirmed(self):
        s = self.result["matching_summary"]
        self.assertEqual((s["strong_pairs"], s["total_pairs"]), (0, 2))
        self.assertEqual(self.result["evidence"]["confirmed_pairs"], [])
        audit_coverage(self.result)

    def test_no_confirmed_pairs_all_records_need_review(self):
        ev = self.result["evidence"]
        self.assertEqual(ev["unmatched"], {"requests": [], "results": []})
        self.assertEqual({r["request_id"] for r in ev["needs_review"]["requests"]},
                         {"REQ-M1", "REQ-T1"})
        self.assertEqual({r["result_id"] for r in ev["needs_review"]["results"]},
                         {"RES-S1", "RES-F1"})

    def test_group_ranges_and_status(self):
        g = self.result["operations"]["groups"]
        self.assertEqual(g["station"]["requests_in_window"], 2)
        self.assertEqual((g["station"]["successes_min"], g["station"]["successes_max"]), (1, 1))
        self.assertEqual(g["station"]["status"], "met")          # 1/2 = 5000bp 达标
        for ch in ("mobile", "terminal"):
            self.assertEqual(g[ch]["requests_in_window"], 1)
            self.assertEqual((g[ch]["successes_min"], g[ch]["successes_max"]), (0, 1))
            self.assertEqual(g[ch]["status"], "undetermined")    # 渠道无法判定

    def test_witnesses(self):
        audit_all_witnesses(self.result, self.requests, self.results)


class StrongEvidenceTests(unittest.TestCase):
    """强证据优先：带码确证不可为凑配对总数而放弃。"""

    def test_coded_fixture(self):
        requests, results = load("coded-requests.json"), load("coded-results.json")
        r = analyze(requests, results,
                    "2026-11-20T11:00:00Z", "2026-11-20T11:02:00Z", 5000)
        s = r["matching_summary"]
        # 放弃确证可凑两对（REQ-K↔RES-X + REQ-N↔RES-K），但强证据数不得降低
        self.assertEqual((s["strong_pairs"], s["total_pairs"]), (1, 1))
        conf = r["evidence"]["confirmed_pairs"]
        self.assertEqual(len(conf), 1)
        self.assertEqual((conf[0]["request_id"], conf[0]["result_id"]), ("REQ-K", "RES-K"))
        self.assertEqual(conf[0]["evidence"], "strong")
        self.assertEqual(conf[0]["outcome"], "started")
        self.assertEqual([x["request_id"] for x in r["evidence"]["unmatched"]["requests"]],
                         ["REQ-N"])
        self.assertEqual([x["result_id"] for x in r["evidence"]["unmatched"]["results"]],
                         ["RES-X"])
        audit_coverage(r)
        audit_all_witnesses(r, requests, results)

    def test_out_of_window_request_occupies_result_via_strong_evidence(self):
        # 窗外带码请求凭强证据占用窗内请求也能匹配的结果：窗内请求只能未配对。
        requests = [req("REQ-IN", "G", 10),                      # 窗内，无码
                    req("REQ-OUT", "G", 130, code="K")]         # 窗外，带码 K
        results = [res("RES-Z", "G", 140, code="K")]            # 两者都能配
        window = (iso(BASE - timedelta(seconds=60)), iso(BASE + timedelta(seconds=100)))
        r = run_analyze(requests, results, window=window)
        self.assertEqual(r["matching_summary"]["strong_pairs"], 1)
        conf = r["evidence"]["confirmed_pairs"]
        self.assertEqual([(p["request_id"], p["result_id"]) for p in conf],
                         [("REQ-OUT", "RES-Z")])
        self.assertEqual([x["request_id"] for x in r["evidence"]["unmatched"]["requests"]],
                         ["REQ-IN"])
        g = r["operations"]["groups"]
        self.assertEqual(g["station"]["requests_in_window"], 1)  # 只有 REQ-IN 在窗内
        self.assertEqual((g["station"]["successes_min"], g["station"]["successes_max"]), (0, 0))
        self.assertEqual(g["station"]["status"], "not_met")

    def test_exactly_120_seconds_is_compatible_121_is_not(self):
        requests = [req("REQ-A", "G", 0)]
        results = [res("RES-120", "G", 120), res("RES-121", "G", 121, outcome="failed")]
        r = run_analyze(requests, results)
        conf = r["evidence"]["confirmed_pairs"]
        self.assertEqual([(p["request_id"], p["result_id"]) for p in conf],
                         [("REQ-A", "RES-120")])
        self.assertEqual([x["result_id"] for x in r["evidence"]["unmatched"]["results"]],
                         ["RES-121"])

    def test_cross_gun_never_compatible(self):
        r = run_analyze([req("REQ-A", "GUN-1", 0)], [res("RES-B", "GUN-2", 10)])
        self.assertEqual(r["matching_summary"]["total_pairs"], 0)
        self.assertEqual([x["request_id"] for x in r["evidence"]["unmatched"]["requests"]],
                         ["REQ-A"])
        self.assertEqual([x["result_id"] for x in r["evidence"]["unmatched"]["results"]],
                         ["RES-B"])

    def test_mismatched_codes_not_compatible(self):
        r = run_analyze([req("REQ-A", "G", 0, code="K")], [res("RES-B", "G", 10, code="L")])
        self.assertEqual(r["matching_summary"]["total_pairs"], 0)
        self.assertEqual([x["request_id"] for x in r["evidence"]["unmatched"]["requests"]],
                         ["REQ-A"])

    def test_one_side_code_is_only_time_evidence(self):
        for q_code, s_code in (("K", None), (None, "K")):
            requests = [req("REQ-A", "G", 0, code=q_code)]
            results = [res("RES-B", "G", 10, code=s_code)]
            r = run_analyze(requests, results)
            self.assertEqual(r["matching_summary"]["strong_pairs"], 0)
            self.assertEqual(r["matching_summary"]["total_pairs"], 1)
            conf = r["evidence"]["confirmed_pairs"]
            self.assertEqual(len(conf), 1)
            self.assertEqual(conf[0]["evidence"], "time")

    def test_correlation_code_is_not_identity_and_may_repeat(self):
        # 同一关联码出现在多条记录上：都构成强证据边，但不强制一一对应。
        requests = [req("REQ-1", "G", 0, code="K"), req("REQ-2", "G", 10, code="K")]
        results = [res("RES-1", "G", 20, code="K"), res("RES-2", "G", 30, code="K")]
        r = run_analyze(requests, results)
        self.assertEqual((r["matching_summary"]["strong_pairs"],
                          r["matching_summary"]["total_pairs"]), (2, 2))
        self.assertEqual(r["evidence"]["confirmed_pairs"], [])  # 对称，无确证
        self.assertEqual(len(r["evidence"]["needs_review"]["requests"]), 2)

    def test_result_before_request_and_zero_delta(self):
        # 结果早于请求：不能关联；恰好同一秒：可以关联。
        r = run_analyze([req("REQ-A", "G", 10)], [res("RES-B", "G", 0)])
        self.assertEqual(r["matching_summary"]["total_pairs"], 0)
        r2 = run_analyze([req("REQ-A", "G", 10)], [res("RES-B", "G", 10)])
        self.assertEqual(r2["matching_summary"]["total_pairs"], 1)

    def test_same_id_string_across_sources_is_allowed_and_distinct(self):
        # 两类记录可以使用相同标识字符串，不视为同一记录。
        r = run_analyze([req("SAME", "G", 0)], [res("SAME", "G", 5)])
        self.assertEqual(r["matching_summary"]["total_pairs"], 1)
        conf = r["evidence"]["confirmed_pairs"]
        self.assertEqual([(p["request_id"], p["result_id"]) for p in conf],
                         [("SAME", "SAME")])

    def test_matched_in_all_but_partner_varies_needs_review(self):
        # 两条请求争一条结果：每条请求都只在部分最优解释中配对。
        r = run_analyze([req("REQ-1", "G", 0), req("REQ-2", "G", 10)],
                        [res("RES-1", "G", 20)])
        self.assertEqual(r["matching_summary"]["total_pairs"], 1)
        self.assertEqual(r["evidence"]["confirmed_pairs"], [])
        self.assertEqual(r["evidence"]["unmatched"], {"requests": [], "results": []})
        self.assertEqual({x["request_id"] for x in r["evidence"]["needs_review"]["requests"]},
                         {"REQ-1", "REQ-2"})
        self.assertEqual([x["result_id"] for x in r["evidence"]["needs_review"]["results"]],
                         ["RES-1"])


class BacklogTests(unittest.TestCase):
    """繁忙枪口积压：32 请求 × 32 结果全部互容，精确结果而非近似。"""

    def setUp(self):
        self.requests = load("backlog-requests.json")
        self.results = load("backlog-results.json")
        self.result = analyze(self.requests, self.results,
                              "2026-11-20T12:00:00Z", "2026-11-20T12:02:00Z", 5000)

    def test_exact_summary(self):
        s = self.result["matching_summary"]
        self.assertEqual((s["requests"], s["results"]), (32, 32))
        self.assertEqual((s["strong_pairs"], s["total_pairs"]), (0, 32))
        self.assertEqual(s["confirmed_pairs"], 0)

    def test_station_fixed_16_channels_range_0_to_16(self):
        g = self.result["operations"]["groups"]
        self.assertEqual(g["station"]["requests_in_window"], 32)
        self.assertEqual((g["station"]["successes_min"], g["station"]["successes_max"]), (16, 16))
        self.assertEqual(g["station"]["status"], "met")  # 16/32 = 5000bp
        for ch in ("mobile", "terminal"):
            self.assertEqual(g[ch]["requests_in_window"], 16)
            self.assertEqual((g[ch]["successes_min"], g[ch]["successes_max"]), (0, 16))
            self.assertEqual(g[ch]["status"], "undetermined")

    def test_no_confirmed_all_records_need_review(self):
        ev = self.result["evidence"]
        self.assertEqual(ev["confirmed_pairs"], [])
        self.assertEqual(ev["unmatched"], {"requests": [], "results": []})
        self.assertEqual(len(ev["needs_review"]["requests"]), 32)
        self.assertEqual(len(ev["needs_review"]["results"]), 32)
        audit_coverage(self.result)

    def test_witnesses_are_complete_and_optimal(self):
        audit_all_witnesses(self.result, self.requests, self.results)
        g = self.result["operations"]["groups"]
        # 每个端点证据都是 32 对的完整配对
        for ch in ("station", "mobile", "terminal"):
            for tag in ("min", "max"):
                self.assertEqual(g[ch][f"witness_{tag}"]["total_pairs"], 32)


class NoDataTests(unittest.TestCase):
    """没有请求的组单列为无数据，不声称达标。"""

    def test_window_without_any_request_is_no_data_everywhere(self):
        requests = [req("REQ-A", "G", 0)]
        results = [res("RES-A", "G", 5)]
        # 窗口落在所有请求之后；观测截止相应后移
        r = analyze(req_doc(requests, cutoff_seconds=7200),
                    res_doc(results, cutoff_seconds=7200),
                    iso(BASE + timedelta(seconds=3600)),
                    iso(BASE + timedelta(seconds=5400)), 5000)
        for ch in ("station", "mobile", "terminal"):
            g = r["operations"]["groups"][ch]
            self.assertEqual(g["requests_in_window"], 0)
            self.assertEqual(g["status"], "no_data")
            self.assertEqual((g["successes_min"], g["successes_max"]), (0, 0))

    def test_channel_without_requests_is_no_data(self):
        requests = [req("REQ-M", "G", 0, channel="mobile")]
        results = [res("RES-M", "G", 5)]
        r = run_analyze(requests, results)
        g = r["operations"]["groups"]
        self.assertEqual(g["terminal"]["status"], "no_data")
        self.assertEqual(g["terminal"]["requests_in_window"], 0)
        self.assertEqual(g["mobile"]["status"], "met")
        self.assertEqual(g["station"]["status"], "met")

    def test_empty_snapshot_is_no_data(self):
        r = run_analyze([], [])
        self.assertEqual(r["matching_summary"]["total_pairs"], 0)
        for ch in ("station", "mobile", "terminal"):
            self.assertEqual(r["operations"]["groups"][ch]["status"], "no_data")
        self.assertEqual(r["evidence"]["confirmed_pairs"], [])


class TargetComparisonTests(unittest.TestCase):
    """目标基点值的整数精确比较边界。"""

    def _one_fixed_success_of_two(self, target_bp):
        # 站点口径固定 1/2 成功（同题面场景一）
        requests = [req("REQ-M", "G", 0, channel="mobile"),
                    req("REQ-T", "G", 20, channel="terminal")]
        results = [res("RES-S", "G", 30), res("RES-F", "G", 40, outcome="failed")]
        return run_analyze(requests, results, target_bp=target_bp)

    def test_exact_hit_is_met(self):
        r = self._one_fixed_success_of_two(5000)   # 1/2 = 5000bp，达到即达标
        self.assertEqual(r["operations"]["groups"]["station"]["status"], "met")

    def test_one_bp_above_is_not_met(self):
        r = self._one_fixed_success_of_two(5001)   # 最高 5000bp < 5001bp
        self.assertEqual(r["operations"]["groups"]["station"]["status"], "not_met")

    def test_one_bp_below_is_met(self):
        r = self._one_fixed_success_of_two(4999)
        self.assertEqual(r["operations"]["groups"]["station"]["status"], "met")

    def test_zero_target_always_met_when_data(self):
        requests = [req("REQ-A", "G", 0)]
        results = [res("RES-F", "G", 5, outcome="failed")]
        r = run_analyze(requests, results, target_bp=0)
        self.assertEqual(r["operations"]["groups"]["station"]["status"], "met")

    def test_full_target_requires_certain_success(self):
        requests = [req("REQ-A", "G", 0)]
        results = [res("RES-S", "G", 5)]
        r = run_analyze(requests, results, target_bp=10000)
        self.assertEqual(r["operations"]["groups"]["station"]["status"], "met")
        r2 = run_analyze(requests, [res("RES-F", "G", 5, outcome="failed")],
                         target_bp=10000)
        self.assertEqual(r2["operations"]["groups"]["station"]["status"], "not_met")

    def test_max_exactly_at_target_is_not_below(self):
        # 渠道 0..1、分母 1：最高成功率恰等于 10000bp，不满足“仍低于目标”。
        requests = [req("REQ-M", "G", 0, channel="mobile"),
                    req("REQ-T", "G", 20, channel="terminal")]
        results = [res("RES-S", "G", 30), res("RES-F", "G", 40, outcome="failed")]
        r = run_analyze(requests, results, target_bp=10000)
        g = r["operations"]["groups"]
        self.assertEqual(g["mobile"]["status"], "undetermined")
        self.assertEqual(g["station"]["status"], "not_met")  # 最高 1/2 < 10000bp


class InvalidInputTests(unittest.TestCase):

    def _expect_invalid(self, requests=None, results=None, window=WINDOW,
                        target_bp=5000, **kw):
        with self.assertRaises(EvidenceError) as ctx:
            run_analyze(requests if requests is not None else [],
                        results if results is not None else [],
                        window=window, target_bp=target_bp, **kw)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_station_mismatch(self):
        with self.assertRaises(EvidenceError) as ctx:
            analyze(req_doc([], station="ST-1"), res_doc([], station="ST-2"),
                    WINDOW[0], WINDOW[1], 5000)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_observed_until_mismatch(self):
        with self.assertRaises(EvidenceError) as ctx:
            analyze(req_doc([], cutoff_seconds=3600), res_doc([], cutoff_seconds=3601),
                    WINDOW[0], WINDOW[1], 5000)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_duplicate_identity_within_source(self):
        self._expect_invalid(requests=[req("A", "G", 0), req("A", "G", 5)])
        self._expect_invalid(results=[res("B", "G", 0), res("B", "G", 5)])

    def test_blank_identifier_rejected(self):
        self._expect_invalid(requests=[req("", "G", 0)])
        self._expect_invalid(requests=[req("   ", "G", 0)])
        self._expect_invalid(requests=[req("A", "  ", 0)])
        self._expect_invalid(results=[res("", "G", 0)])

    def test_naive_and_subsecond_timestamps_rejected(self):
        bad = req("A", "G", 0)
        bad["requested_at"] = "2026-11-20T10:00:00"   # 无偏移
        self._expect_invalid(requests=[bad])
        bad2 = req("A", "G", 0)
        bad2["requested_at"] = "2026-11-20T10:00:00.500+00:00"  # 亚秒
        self._expect_invalid(requests=[bad2])

    def test_record_after_observed_until_rejected(self):
        self._expect_invalid(requests=[req("A", "G", 7200)], cutoff_seconds=3600)
        self._expect_invalid(results=[res("B", "G", 7200)], cutoff_seconds=3600)

    def test_unknown_and_missing_fields_rejected(self):
        bad = req("A", "G", 0)
        bad["bogus"] = 1
        self._expect_invalid(requests=[bad])
        bad2 = req("A", "G", 0)
        del bad2["channel"]
        self._expect_invalid(requests=[bad2])
        with self.assertRaises(EvidenceError):
            analyze({"station_id": "S", "requests": []}, res_doc([]),
                    WINDOW[0], WINDOW[1], 5000)

    def test_bad_channel_and_outcome(self):
        self._expect_invalid(requests=[req("A", "G", 0, channel="phone")])
        self._expect_invalid(results=[res("B", "G", 0, outcome="unknown")])

    def test_bad_correlation_code_type(self):
        bad = req("A", "G", 0)
        bad["correlation_code"] = 7
        self._expect_invalid(requests=[bad])

    def test_window_must_be_non_empty_and_cutoff_must_cover_wait(self):
        self._expect_invalid(window=(iso(BASE), iso(BASE)))                # 空窗口
        self._expect_invalid(window=(iso(BASE + timedelta(seconds=10)),    # 倒窗
                                     iso(BASE)))
        # 观测截止早于窗口结束后 120 秒
        with self.assertRaises(EvidenceError) as ctx:
            analyze(req_doc([], cutoff_seconds=100), res_doc([], cutoff_seconds=100),
                    iso(BASE), iso(BASE + timedelta(seconds=60)), 5000)
        self.assertEqual(ctx.exception.code, "invalid_input")

    def test_target_bp_range_and_type(self):
        for bad in (-1, 10001, "5000", 50.0, True, None):
            self._expect_invalid(target_bp=bad)

    def test_top_level_structure(self):
        with self.assertRaises(EvidenceError):
            analyze([1, 2], res_doc([]), WINDOW[0], WINDOW[1], 5000)
        with self.assertRaises(EvidenceError):
            analyze(req_doc([]), res_doc([]), "not-a-time", WINDOW[1], 5000)


class DeterminismTests(unittest.TestCase):
    """调换日志行序后，证据清单、统计与证明配对逐字节一致。"""

    def _check(self, req_name, res_name, window):
        requests, results = load(req_name), load(res_name)
        baseline = json.dumps(analyze(requests, results, window[0], window[1], 5000),
                              sort_keys=True)
        rng = random.Random(20261120)
        for _ in range(5):
            r2 = json.loads(json.dumps(requests))
            s2 = json.loads(json.dumps(results))
            rng.shuffle(r2["requests"])
            rng.shuffle(s2["results"])
            got = json.dumps(analyze(r2, s2, window[0], window[1], 5000),
                             sort_keys=True)
            self.assertEqual(baseline, got)

    def test_two_channel(self):
        self._check("two-channel-requests.json", "two-channel-results.json",
                    ("2026-11-20T10:00:00Z", "2026-11-20T10:02:00Z"))

    def test_coded(self):
        self._check("coded-requests.json", "coded-results.json",
                    ("2026-11-20T11:00:00Z", "2026-11-20T11:02:00Z"))

    def test_backlog(self):
        self._check("backlog-requests.json", "backlog-results.json",
                    ("2026-11-20T12:00:00Z", "2026-11-20T12:02:00Z"))

    def test_offset_representation_does_not_matter(self):
        # 同一瞬时的不同偏移写法得到相同规范化输出
        requests = [req("A", "G", 0)]
        results = [res("B", "G", 5)]
        a = run_analyze(requests, results)
        shifted = dict(results[0])
        shifted["resolved_at"] = "2026-11-20T11:00:05+01:00"  # 同一瞬时
        b = run_analyze(requests, [shifted])
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


class BruteForceCrossCheckTests(unittest.TestCase):
    """与实现完全独立的暴力枚举交叉校验（随机小实例）。"""

    def _brute(self, requests, results, ws, we):
        n, m = len(requests), len(results)
        compat = {i: [] for i in range(n)}
        for i, q in enumerate(requests):
            qt = parse(q["requested_at"])
            for j, s in enumerate(results):
                st = parse(s["resolved_at"])
                if q["gun_id"] != s["gun_id"]:
                    continue
                if not (0 <= (st - qt).total_seconds() <= 120):
                    continue
                qc, sc = q.get("correlation_code"), s.get("correlation_code")
                if qc and sc and qc != sc:
                    continue
                compat[i].append(j)
        best_key = None
        opts = []

        def rec(i, used, pairs):
            nonlocal best_key
            if i == n:
                strong = sum(1 for a, b in pairs
                             if requests[a].get("correlation_code")
                             and results[b].get("correlation_code"))
                key = (strong, len(pairs))
                if best_key is None or key > best_key:
                    best_key = key
                    opts.clear()
                    opts.append(list(pairs))
                elif key == best_key:
                    opts.append(list(pairs))
                return
            rec(i + 1, used, pairs)
            for j in compat[i]:
                if j not in used:
                    used.add(j)
                    pairs.append((i, j))
                    rec(i + 1, used, pairs)
                    pairs.pop()
                    used.discard(j)

        rec(0, set(), [])
        return opts

    def test_random_small_instances(self):
        rng = random.Random(7717)
        guns = ["G1", "G2"]
        codes = [None, None, "K"]
        channels = ["mobile", "terminal"]
        outcomes = ["started", "failed"]
        ws = BASE
        we = BASE + timedelta(seconds=200)
        for _ in range(120):
            n = rng.randint(0, 4)
            m = rng.randint(0, 4)
            requests = [req(f"R{i}", rng.choice(guns), rng.randint(-30, 230),
                            channel=rng.choice(channels), code=rng.choice(codes))
                        for i in range(n)]
            results = [res(f"S{j}", rng.choice(guns), rng.randint(-30, 230),
                           outcome=rng.choice(outcomes), code=rng.choice(codes))
                       for j in range(m)]
            got = analyze(req_doc(requests, cutoff_seconds=400),
                          res_doc(results, cutoff_seconds=400),
                          iso(ws), iso(we), 5000)
            opts = self._brute(requests, results, ws, we)

            # 汇总口径
            strong0 = sum(1 for a, b in opts[0]
                          if requests[a].get("correlation_code")
                          and results[b].get("correlation_code"))
            self.assertEqual(got["matching_summary"]["strong_pairs"], strong0)
            self.assertEqual(got["matching_summary"]["total_pairs"], len(opts[0]))

            # 确证 = 每份最优解释都出现的配对
            in_all = set(opts[0])
            for o in opts[1:]:
                in_all &= set(o)
            got_conf = {(p["request_id"], p["result_id"])
                        for p in got["evidence"]["confirmed_pairs"]}
            expect_conf = {(requests[a]["request_id"], results[b]["result_id"])
                           for a, b in in_all}
            self.assertEqual(got_conf, expect_conf)

            # 未配对 = 任何最优解释都不配对
            in_some = set().union(*[set(o) for o in opts])
            req_some = {requests[a]["request_id"] for a, _ in in_some}
            res_some = {results[b]["result_id"] for _, b in in_some}
            got_un_req = {x["request_id"] for x in got["evidence"]["unmatched"]["requests"]}
            got_un_res = {x["result_id"] for x in got["evidence"]["unmatched"]["results"]}
            self.assertEqual(got_un_req, {q["request_id"] for q in requests}
                             - req_some)
            self.assertEqual(got_un_res, {s["result_id"] for s in results}
                             - res_some)
            audit_coverage(got)

            # 每组成功数区间
            def successes(o, group):
                c = 0
                for a, b in o:
                    q = requests[a]
                    if not (ws <= parse(q["requested_at"]) < we):
                        continue
                    if group != "station" and q["channel"] != group:
                        continue
                    if results[b]["outcome"] == "started":
                        c += 1
                return c

            for group in ("station", "mobile", "terminal"):
                vals = [successes(o, group) for o in opts]
                g = got["operations"]["groups"][group]
                denom = sum(1 for q in requests
                            if ws <= parse(q["requested_at"]) < we
                            and (group == "station" or q["channel"] == group))
                self.assertEqual(g["requests_in_window"], denom)
                self.assertEqual(g["successes_min"], min(vals))
                self.assertEqual(g["successes_max"], max(vals))
            audit_all_witnesses(got, req_doc(requests), res_doc(results))


class CliEndToEndTests(unittest.TestCase):

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "charging.evidence.cli", *args],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)

    def test_cli_success_writes_result_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence.json"
            r = self._cli("--requests", str(FIX / "two-channel-requests.json"),
                          "--results", str(FIX / "two-channel-results.json"),
                          "--window-start", "2026-11-20T10:00:00Z",
                          "--window-end", "2026-11-20T10:02:00Z",
                          "--target-bp", "5000",
                          "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            payload = json.loads(out.read_text())
            self.assertEqual(payload["evidence_version"], EVIDENCE_VERSION)
            self.assertEqual(payload["operations"]["groups"]["station"]["status"], "met")
            summary = json.loads(r.stdout)
            self.assertEqual(summary["total_pairs"], 2)
            self.assertEqual(summary["status"]["mobile"], "undetermined")

    def test_cli_no_data_group(self):
        # 真实 CLI 调用：窗口内没有任何请求 -> 三组都单列 no_data，不声称达标
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            req_path = tmp / "requests.json"
            res_path = tmp / "results.json"
            req_path.write_text(json.dumps(req_doc(
                [req("REQ-A", "G", 0)], cutoff_seconds=7200)))
            res_path.write_text(json.dumps(res_doc(
                [res("RES-A", "G", 5)], cutoff_seconds=7200)))
            out = tmp / "evidence.json"
            r = self._cli("--requests", str(req_path),
                          "--results", str(res_path),
                          "--window-start", iso(BASE + timedelta(seconds=3600)),
                          "--window-end", iso(BASE + timedelta(seconds=5400)),
                          "--target-bp", "5000",
                          "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            payload = json.loads(out.read_text())
            for ch in ("station", "mobile", "terminal"):
                g = payload["operations"]["groups"][ch]
                self.assertEqual(g["status"], "no_data")
                self.assertEqual(g["requests_in_window"], 0)

    def test_cli_cutoff_must_cover_result_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence.json"
            # 观测截止 10:04:00 早于窗口结束 10:02:00 后 120 秒：非法
            r = self._cli("--requests", str(FIX / "two-channel-requests.json"),
                          "--results", str(FIX / "two-channel-results.json"),
                          "--window-start", "2026-11-20T10:00:00Z",
                          "--window-end", "2026-11-20T10:04:00Z",
                          "--target-bp", "5000",
                          "--output", str(out))
            self.assertEqual(r.returncode, 2)
            self.assertIn("invalid_input", r.stderr)
            self.assertFalse(out.exists())

    def test_cli_invalid_input_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence.json"
            good = self._cli("--requests", str(FIX / "two-channel-requests.json"),
                             "--results", str(FIX / "two-channel-results.json"),
                             "--window-start", "2026-11-20T10:00:00Z",
                             "--window-end", "2026-11-20T10:02:00Z",
                             "--target-bp", "5000",
                             "--output", str(out))
            self.assertEqual(good.returncode, 0, good.stderr)
            before = out.read_text()

            bad_results = json.loads(load("two-channel-results.json") and
                                     (FIX / "two-channel-results.json").read_text())
            bad_results["station_id"] = "ST-OTHER"
            bad_path = Path(tmp) / "bad-results.json"
            bad_path.write_text(json.dumps(bad_results))
            bad = self._cli("--requests", str(FIX / "two-channel-requests.json"),
                            "--results", str(bad_path),
                            "--window-start", "2026-11-20T10:00:00Z",
                            "--window-end", "2026-11-20T10:02:00Z",
                            "--target-bp", "5000",
                            "--output", str(out))
            self.assertEqual(bad.returncode, 2)
            self.assertIn("invalid_input", bad.stderr)
            self.assertEqual(out.read_text(), before)  # 原结果文件保留

    def test_cli_missing_file_fails_without_touching_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence.json"
            r = self._cli("--requests", str(Path(tmp) / "nope.json"),
                          "--results", str(FIX / "two-channel-results.json"),
                          "--window-start", "2026-11-20T10:00:00Z",
                          "--window-end", "2026-11-20T10:02:00Z",
                          "--target-bp", "5000",
                          "--output", str(out))
            self.assertEqual(r.returncode, 2)
            self.assertFalse(out.exists())

    def test_cli_is_read_only_for_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence.json"
            req_src = (FIX / "coded-requests.json").read_text()
            res_src = (FIX / "coded-results.json").read_text()
            r = self._cli("--requests", str(FIX / "coded-requests.json"),
                          "--results", str(FIX / "coded-results.json"),
                          "--window-start", "2026-11-20T11:00:00Z",
                          "--window-end", "2026-11-20T11:02:00Z",
                          "--target-bp", "5000",
                          "--output", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            # 输入日志逐字节不变：本入口只读日志
            self.assertEqual((FIX / "coded-requests.json").read_text(), req_src)
            self.assertEqual((FIX / "coded-results.json").read_text(), res_src)


if __name__ == "__main__":
    unittest.main()
