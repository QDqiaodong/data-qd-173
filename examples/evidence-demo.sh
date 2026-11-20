#!/usr/bin/env bash
# 充电启动证据核验的可运行示例。
#
# 用法（在项目根目录执行）：
#   bash examples/evidence-demo.sh
#
# 在临时目录中串联三个代表性场景，并演示非法输入的失败语义：
#   1) 两渠道时间歧义：站点 1..1 达标，渠道 0..1 无法判定，无确证配对
#   2) 强证据优先：带码确证不可为凑配对总数而放弃
#   3) 繁忙枪口积压：32x32 全互容，站点固定 16、渠道 0..16，无确证
#   4) 非法输入（站点不一致）-> 退出 2，已写结果保留
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX="$ROOT/fixtures/evidence"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/evidence.json"

run() {
  python3 -m charging.evidence.cli \
    --requests "$FIX/$1" --results "$FIX/$2" \
    --window-start "$3" --window-end "$4" --target-bp "$5" \
    --output "$OUT"
}

( cd "$ROOT"
  echo "== 1) 两渠道时间歧义：站点 1..1（5000bp 达标），渠道 0..1 无法判定 =="
  run two-channel-requests.json two-channel-results.json \
      2026-11-20T10:00:00Z 2026-11-20T10:02:00Z 5000
  python3 -c "
import json
r = json.load(open('$OUT'))
print('  确证配对:', r['evidence']['confirmed_pairs'])
print('  有待人工核查的请求:', [x['request_id'] for x in r['evidence']['needs_review']['requests']])
"
  echo

  echo "== 2) 强证据优先：只确证带码配对（强证据 1、总配对 1，不凑成 2 对） =="
  run coded-requests.json coded-results.json \
      2026-11-20T11:00:00Z 2026-11-20T11:02:00Z 5000
  python3 -c "
import json
r = json.load(open('$OUT'))
print('  确证配对:', [(p['request_id'], p['result_id'], p['evidence']) for p in r['evidence']['confirmed_pairs']])
print('  未配对请求:', [x['request_id'] for x in r['evidence']['unmatched']['requests']])
"
  echo

  echo "== 3) 繁忙枪口积压：32x32 全互容，站点固定 16、渠道 0..16、无确证 =="
  run backlog-requests.json backlog-results.json \
      2026-11-20T12:00:00Z 2026-11-20T12:02:00Z 5000
  python3 -c "
import json
r = json.load(open('$OUT'))
g = r['operations']['groups']
for name in ('station', 'mobile', 'terminal'):
    v = g[name]
    print(f\"  {name}: 窗内请求 {v['requests_in_window']} 成功 {v['successes_min']}..{v['successes_max']} -> {v['status']}\")
"
  echo

  echo "== 4) 非法输入：两份日志站点不一致（预期退出 2，结果文件保留） =="
  cp "$OUT" "$TMP/before.json"
  python3 -c "
import json
d = json.load(open('$FIX/two-channel-results.json'))
d['station_id'] = 'ST-OTHER'
json.dump(d, open('$TMP/bad-results.json', 'w'))
"
  if python3 -m charging.evidence.cli \
      --requests "$FIX/two-channel-requests.json" \
      --results "$TMP/bad-results.json" \
      --window-start 2026-11-20T10:00:00Z --window-end 2026-11-20T10:02:00Z \
      --target-bp 5000 --output "$OUT"; then
    echo "!! 预期失败却成功"; exit 1
  else
    code=$?
    echo "   退出码 $code（预期 2）"
  fi
  cmp "$OUT" "$TMP/before.json" && echo "   已有结果文件逐字节保留"
)
echo "== 全部演示完成 =="
