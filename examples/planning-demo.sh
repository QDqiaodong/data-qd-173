#!/usr/bin/env bash
# 车队补能 / 电网降载邀约联合排程的可运行示例。
#
# 用法（在项目根目录执行）：
#   bash examples/planning-demo.sh
#
# 在临时目录中串联四个代表性场景，并演示两类可区分失败：
#   1) 题面冲突：两项互斥前缀邀约，只签报酬更高的 O2
#   2) 同报酬：按邀约标识字典序裁决，改签 O1
#   3) 短时降载后补回电量（recovery）
#   4) 多项合计报酬压过最值钱单项（combo）
#   5) 非法输入（扣减超过正常额度）-> 退出 2，已写结果保留
#   6) 合法但常态不可行 -> 退出 2，错误码 baseline_infeasible
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX="$ROOT/fixtures/planning"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/plan.json"

run() {
  local fleet="$1"; local offers="$2"
  python3 -m charging.planning.cli \
    --fleet "$FIX/$fleet" --offers "$FIX/$offers" --output "$OUT"
}

( cd "$ROOT"
  echo "== 1) 题面冲突：O1 报酬 7、O2 报酬 9，只能二选一 =="
  run fleet-headline.json offers-headline.json
  echo

  echo "== 2) 同报酬 9：按标识字典序裁决，签 O1 =="
  run fleet-headline.json offers-headline-tie.json
  echo

  echo "== 3) 短时降载：电量在降载结束后的时段补回 =="
  run fleet-recovery.json offers-recovery.json
  echo

  echo "== 4) 组合压过单项：S1+S2 合计 12 > BIG 单项 10 =="
  run fleet-combo.json offers-combo.json
  echo

  echo "== 5) 非法输入：扣减超过正常额度（预期退出 2，结果文件保留） =="
  cp "$OUT" "$TMP/before.json"
  if python3 -m charging.planning.cli \
      --fleet "$FIX/fleet-headline.json" \
      --offers "$FIX/offers-illegal-reduction.json" \
      --output "$OUT"; then
    echo "ERROR: 本次应当失败" >&2
    exit 1
  else
    echo "已按预期拒绝（invalid_input，退出码 2）"
  fi
  cmp -s "$OUT" "$TMP/before.json" && echo "输出文件与失败前字节一致"
  echo

  echo "== 6) 合法但常态不可行（预期退出 2，码 baseline_infeasible） =="
  if python3 -m charging.planning.cli \
      --fleet "$FIX/fleet-infeasible.json" \
      --offers "$FIX/offers-infeasible.json" \
      --output "$TMP/never.json"; then
    echo "ERROR: 本次应当失败" >&2
    exit 1
  else
    echo "已按预期拒绝（baseline_infeasible，退出码 2）"
  fi
  test ! -e "$TMP/never.json" && echo "未创建任何输出文件"
)

echo
echo "全部示例完成。临时目录：$TMP"
